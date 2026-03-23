"""Campaign Worker — Stateless polling-based email scheduler.

Architecture:
  - On campaign launch, pre-compute scheduled_at for ALL emails (with randomized delays + business hours)
  - A single background thread polls every 30s for emails ready to send
  - Completely stateless — survives pod restarts without losing progress
  - No per-campaign threads, no sleeping for hours

Scheduling rules:
  - First email: 30-180 seconds after launch (ignores business hours — trust signal)
  - All subsequent emails: business hours only (9am-6pm user timezone)
  - Daily limit: 5-7 emails per day (randomized)
  - Within each day, send times are randomly distributed between 9am-6pm
"""

import random
import threading
import time
from datetime import datetime, timedelta

import pytz

from core.logger import get_logger
from database.session import SessionLocal
from database.models import Campaign, EmailSent, EmailAccount
from services.email_campaign.gmail_send_service import send_gmail_email, _refresh_token_sync

logger = get_logger(__name__)

_sender_thread: threading.Thread | None = None
_sender_stop = threading.Event()

POLL_INTERVAL = 30  # seconds between poll cycles
DAILY_LIMIT_MIN = 5
DAILY_LIMIT_MAX = 7


# ── Schedule Computation ─────────────────────────────────────────────────────

def compute_campaign_schedule(db, campaign_id: int):
    """Pre-compute scheduled_at for all queued emails in a campaign.

    Called when campaign transitions to 'running'.

    Algorithm:
      1. First email: 30-180s from now (ignores business hours — trust signal)
      2. All remaining emails are distributed across days:
         - Each day gets random(5, 7) emails
         - Send times are randomly placed between 9am-6pm (user timezone)
         - Days are consecutive starting from the next business day
    """
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        return

    tz_name = campaign.user_timezone or "Asia/Kolkata"
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone("Asia/Kolkata")

    emails = (
        db.query(EmailSent)
        .filter(EmailSent.campaign_id == campaign_id, EmailSent.status == "queued")
        .order_by(EmailSent.id.asc())
        .all()
    )

    if not emails:
        return

    now_utc = datetime.utcnow()

    # ── First email: 30-180 seconds from now (ignores business hours) ──
    first_delay = random.uniform(30, 180)
    emails[0].scheduled_at = now_utc + timedelta(seconds=first_delay)

    # ── Remaining emails: distribute across days within business hours ──
    remaining = emails[1:]
    if not remaining:
        db.commit()
        logger.info("[SCHEDULER] Computed schedule for campaign %d: 1 email at %s",
                    campaign_id, emails[0].scheduled_at)
        return

    # Determine the first scheduling day (next business-hours window)
    now_local = now_utc.replace(tzinfo=pytz.utc).astimezone(tz)
    # If we're before 6pm today, start scheduling today; otherwise tomorrow
    if now_local.hour < 18:
        current_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        current_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    idx = 0
    while idx < len(remaining):
        daily_limit = random.randint(DAILY_LIMIT_MIN, DAILY_LIMIT_MAX)
        day_batch = remaining[idx:idx + daily_limit]

        # Generate random times between 9:00-17:30 for this day (leave 30min buffer before 18:00)
        random_minutes = sorted([random.uniform(0, 510) for _ in range(len(day_batch))])  # 0-510 min = 9:00-17:30

        for j, email in enumerate(day_batch):
            send_local = current_day.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(minutes=random_minutes[j])
            # Convert to UTC and strip tzinfo for DB storage
            send_utc = send_local.astimezone(pytz.utc).replace(tzinfo=None)
            email.scheduled_at = send_utc

        idx += len(day_batch)
        current_day += timedelta(days=1)  # Move to next day

    db.commit()
    logger.info("[SCHEDULER] Computed schedule for campaign %d: %d emails, first at %s, last at %s, spanning %d days",
                campaign_id, len(emails), emails[0].scheduled_at, emails[-1].scheduled_at,
                (emails[-1].scheduled_at - emails[0].scheduled_at).days + 1)


def _push_to_business_hours(dt_utc: datetime, tz) -> datetime:
    """If dt_utc falls outside 9am-6pm in the given timezone, push it to the next 9am."""
    local = dt_utc.replace(tzinfo=pytz.utc).astimezone(tz)
    hour = local.hour

    if 9 <= hour < 18:
        return dt_utc  # Already in business hours

    # Push to next 9am
    if hour >= 18:
        next_9am_local = local.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        next_9am_local = local.replace(hour=9, minute=0, second=0, microsecond=0)

    # Add small random offset (0-15 min) so emails don't all start at exactly 9am
    next_9am_local = next_9am_local + timedelta(minutes=random.uniform(0, 15))

    return next_9am_local.astimezone(pytz.utc).replace(tzinfo=None)


def shift_schedule_forward(db, campaign_id: int, shift_seconds: float):
    """Shift all future scheduled_at timestamps forward (used after resume from pause)."""
    now = datetime.utcnow()
    shift = timedelta(seconds=shift_seconds)

    emails = (
        db.query(EmailSent)
        .filter(
            EmailSent.campaign_id == campaign_id,
            EmailSent.status == "queued",
            EmailSent.scheduled_at.isnot(None),
            EmailSent.scheduled_at > now,
        )
        .all()
    )

    for email in emails:
        email.scheduled_at = email.scheduled_at + shift

    db.commit()
    logger.info("[SCHEDULER] Shifted %d emails forward by %.0f seconds for campaign %d",
                len(emails), shift_seconds, campaign_id)


# ── Polling Sender Loop ──────────────────────────────────────────────────────

def _sender_loop():
    """Background thread: polls for scheduled emails ready to send every POLL_INTERVAL seconds.

    Completely stateless — reads from DB, sends, updates DB. Safe across pod restarts.
    """
    logger.info("[SENDER] Email sender loop started (poll every %ds)", POLL_INTERVAL)

    while not _sender_stop.is_set():
        try:
            _process_ready_emails()
        except Exception as e:
            logger.error("[SENDER] Error in sender loop: %s", e, exc_info=True)

        # Sleep in small chunks so we can stop quickly
        for _ in range(POLL_INTERVAL):
            if _sender_stop.is_set():
                break
            time.sleep(1)

    logger.info("[SENDER] Email sender loop stopped")


def _process_ready_emails():
    """Find and send all emails where scheduled_at <= now and status = queued.

    Returns (sent_count, failed_count) tuple.
    """
    db = SessionLocal()
    sent_count = 0
    failed_count = 0
    try:
        now = datetime.utcnow()

        # Find emails ready to send (across ALL running campaigns)
        ready_emails = (
            db.query(EmailSent)
            .join(Campaign, EmailSent.campaign_id == Campaign.id)
            .filter(
                Campaign.status == "running",
                EmailSent.status == "queued",
                EmailSent.scheduled_at.isnot(None),
                EmailSent.scheduled_at <= now,
            )
            .order_by(EmailSent.scheduled_at.asc())
            .limit(5)  # Process max 5 per cycle to avoid blocking
            .all()
        )

        if not ready_emails:
            return sent_count, failed_count

        logger.info("[SENDER] Found %d emails ready to send", len(ready_emails))

        # Group by campaign for token caching
        token_cache: dict = {}  # email_account_id -> access_token

        for email in ready_emails:
            campaign = db.query(Campaign).filter_by(id=email.campaign_id).first()
            if not campaign or campaign.status != "running":
                continue

            account = db.query(EmailAccount).filter_by(id=campaign.email_account_id).first()
            if not account:
                email.status = "failed"
                email.error_message = "Email account not found"
                db.commit()
                failed_count += 1
                continue

            # Get/refresh access token
            acct_id = account.id
            if acct_id not in token_cache:
                try:
                    token_cache[acct_id] = _refresh_token_sync(account, db)
                except Exception as e:
                    logger.error("[SENDER] Token refresh failed for account %d: %s", acct_id, e)
                    email.status = "failed"
                    email.error_message = f"Token refresh failed: {str(e)[:200]}"
                    db.commit()
                    failed_count += 1
                    continue

            access_token = token_cache[acct_id]

            # Send the email
            try:
                logger.info("[SENDER] Sending email %d (campaign %d) to %s",
                            email.id, email.campaign_id, email.to_email)

                result = send_gmail_email(
                    access_token=access_token,
                    to_email=email.to_email,
                    subject=email.subject,
                    body=email.body,
                    from_email=account.email_address,
                )

                email.status = "sent"
                email.sent_at = datetime.utcnow()
                email.message_id = result.get("id")
                db.commit()
                sent_count += 1

                logger.info("[SENDER] Email %d sent successfully (gmail_id=%s)", email.id, result.get("id"))

            except Exception as e:
                logger.error("[SENDER] Email %d failed: %s", email.id, e)
                email.status = "failed"
                email.error_message = str(e)[:500]
                db.commit()
                failed_count += 1

        # Check if any campaigns are now complete
        _check_campaign_completion(db)

    finally:
        db.close()

    return sent_count, failed_count


def _check_campaign_completion(db):
    """Mark campaigns as completed if all emails are sent/failed."""
    from sqlalchemy import func

    running_campaigns = db.query(Campaign).filter_by(status="running").all()
    for campaign in running_campaigns:
        remaining = db.query(func.count(EmailSent.id)).filter(
            EmailSent.campaign_id == campaign.id,
            EmailSent.status == "queued",
        ).scalar() or 0

        if remaining == 0:
            campaign.status = "completed"
            db.commit()
            logger.info("[SENDER] Campaign %d completed (no more queued emails)", campaign.id)


# ── Public API ────────────────────────────────────────────────────────────────

def start_sender_loop():
    """Start the background sender loop. Called once at app startup."""
    global _sender_thread
    if _sender_thread and _sender_thread.is_alive():
        logger.warning("[SENDER] Sender loop already running")
        return

    _sender_stop.clear()
    _sender_thread = threading.Thread(
        target=_sender_loop,
        daemon=True,
        name="email-sender-loop",
    )
    _sender_thread.start()
    logger.info("[SENDER] Background sender loop started")


def stop_sender_loop():
    """Stop the background sender loop."""
    _sender_stop.set()
    logger.info("[SENDER] Stop signal sent to sender loop")


# Legacy API — kept for backward compat with campaign_service.py imports
def start_campaign_worker(campaign_id: int):
    """No-op in the new architecture. Schedule is computed at launch time."""
    logger.info("[SCHEDULER] start_campaign_worker called for campaign %d (schedule already computed)", campaign_id)


def stop_campaign_worker(campaign_id: int):
    """No-op in the new architecture. Pausing just stops email processing via status check."""
    logger.info("[SCHEDULER] stop_campaign_worker called for campaign %d", campaign_id)
