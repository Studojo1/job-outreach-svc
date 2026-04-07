"""Campaign Worker — JIT enrichment + email generation + sending.

Architecture (Just-In-Time):
  - On campaign launch, pre-compute scheduled_at for ALL placeholder emails
  - A single background thread polls every 30s and runs a 3-phase cycle:
    Phase 1: Enrich upcoming leads (3-hour lookahead, max 3 per cycle)
    Phase 2: Generate email content for enriched leads (max 2 per cycle)
    Phase 3: Send ready emails (max 5 per cycle)
  - Completely stateless — survives pod restarts without losing progress
  - Total cycle budget: ~3.6s worst case (under 5s k8s health check limit)

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
from sqlalchemy import func

from core.logger import get_logger
from core.analytics import capture as ph_capture
from database.session import SessionLocal
from database.models import (
    Campaign, EmailSent, EmailAccount, Lead, LeadScore,
    Candidate, OutreachOrder, UserCredit,
)
from services.email_campaign.gmail_send_service import send_gmail_email, _refresh_token_sync
from services.email_campaign.gmail_inbox_service import (
    list_inbox_messages,
    get_message_detail,
    is_bounce_message,
    extract_bounce_reason,
)
from services.email_campaign.reply_classifier_service import classify_reply_sentiment

logger = get_logger(__name__)

_sender_thread: threading.Thread | None = None
_sender_stop = threading.Event()

POLL_INTERVAL = 30  # seconds between poll cycles
DAILY_LIMIT_MIN = 5
DAILY_LIMIT_MAX = 7
JIT_LOOKAHEAD_HOURS = 3  # enrich/generate leads this far ahead of send time
MAX_ENRICH_PER_CYCLE = 3  # ~0.6s at 0.2s Apollo rate limit
MAX_GENERATE_PER_CYCLE = 2  # ~2s for LLM calls
MAX_SEND_PER_CYCLE = 5  # ~1s for Gmail API calls
MAX_ENRICHMENT_FAILURES = 3  # skip lead after this many failures
REPLY_CHECK_INTERVAL = 300  # 5 minutes between reply checks
_last_reply_check: float = 0.0  # module-level timestamp for throttling


# ── Schedule Computation ─────────────────────────────────────────────────────

def compute_campaign_schedule(db, campaign_id: int):
    """Pre-compute scheduled_at for all pending emails in a campaign.

    Called when campaign transitions to 'running'.
    Works with both 'pending_enrichment' (JIT) and 'queued' (legacy) statuses.

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

    # Support both JIT (pending_enrichment) and legacy (queued) statuses
    emails = (
        db.query(EmailSent)
        .filter(
            EmailSent.campaign_id == campaign_id,
            EmailSent.status.in_(["pending_enrichment", "queued"]),
        )
        .outerjoin(Lead, EmailSent.lead_id == Lead.id)
        .outerjoin(LeadScore, LeadScore.lead_id == Lead.id)
        .order_by(LeadScore.overall_score.desc().nullslast(), EmailSent.id.asc())
        .all()
    )

    if not emails:
        return

    now_utc = datetime.utcnow()

    # ── First email: 30-180 seconds from now (ignores business hours) ──
    first_delay = random.uniform(30, 180)
    emails[0].scheduled_at = now_utc + timedelta(seconds=first_delay)

    # ── Remaining emails: sequential with random 40-90 min gaps ──
    remaining = emails[1:]
    if not remaining:
        db.commit()
        logger.info("[SCHEDULER] Computed schedule for campaign %d: 1 email at %s",
                    campaign_id, emails[0].scheduled_at)
        return

    # Walk forward from the first email's time, adding random gaps.
    # Roll to next day at 9 AM when we hit the daily limit or pass 5 PM.
    cursor_utc = emails[0].scheduled_at
    end_of_day_hour = 17  # 5 PM local — stop scheduling, roll to next day
    daily_count = 1       # First email already counts for today
    daily_limit = random.randint(DAILY_LIMIT_MIN, DAILY_LIMIT_MAX)

    for email in remaining:
        # Random gap: 40-90 minutes after the previous email
        gap_minutes = random.uniform(40, 90)
        cursor_utc = cursor_utc + timedelta(minutes=gap_minutes)

        # Check if we've exceeded today's limit or gone past business hours
        cursor_local = cursor_utc.replace(tzinfo=pytz.utc).astimezone(tz)
        if daily_count >= daily_limit or cursor_local.hour >= end_of_day_hour:
            # Roll to next day at 9:00 AM + small random offset (0-30 min)
            next_day = cursor_local.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
            next_day = next_day + timedelta(minutes=random.uniform(0, 30))
            cursor_utc = next_day.astimezone(pytz.utc).replace(tzinfo=None)
            daily_count = 0
            daily_limit = random.randint(DAILY_LIMIT_MIN, DAILY_LIMIT_MAX)

        email.scheduled_at = cursor_utc
        daily_count += 1

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

    # Shift both pending_enrichment and queued emails
    emails = (
        db.query(EmailSent)
        .filter(
            EmailSent.campaign_id == campaign_id,
            EmailSent.status.in_(["queued", "pending_enrichment"]),
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


# ── JIT Phase 1: Enrich Upcoming Leads ──────────────────────────────────────

def _enrich_upcoming(db) -> int:
    """Enrich leads scheduled within the lookahead window.

    Finds pending_enrichment emails with scheduled_at within the next few hours,
    calls Apollo People Match API for each lead, and updates the email record.

    Returns number of leads successfully enriched.
    """
    from services.enrichment.enrichment_service import _enrich_single_lead

    now = datetime.utcnow()
    lookahead = now + timedelta(hours=JIT_LOOKAHEAD_HOURS)

    # Find emails needing enrichment within the lookahead window
    pending_emails = (
        db.query(EmailSent)
        .join(Campaign, EmailSent.campaign_id == Campaign.id)
        .join(Lead, EmailSent.lead_id == Lead.id)
        .filter(
            Campaign.status == "running",
            EmailSent.enrichment_status == "pending",
            EmailSent.scheduled_at.isnot(None),
            EmailSent.scheduled_at <= lookahead,
        )
        .order_by(EmailSent.scheduled_at.asc())
        .limit(MAX_ENRICH_PER_CYCLE)
        .all()
    )

    if not pending_emails:
        return 0

    enriched_count = 0

    for email in pending_emails:
        lead = db.query(Lead).filter_by(id=email.lead_id).first()
        if not lead:
            email.enrichment_status = "skipped"
            email.status = "failed"
            email.error_message = "Lead not found"
            db.commit()
            continue

        # Lead already enriched (e.g., by preview enrichment) — reuse email, still deduct credit
        if lead.email and lead.email_verified:
            email.to_email = lead.email
            email.enrichment_status = "enriched"
            _deduct_single_credit(db, email)
            db.commit()
            enriched_count += 1
            continue

        try:
            result = _enrich_single_lead(lead)

            if result:
                # Update lead
                lead.email = result["email"]
                if result.get("name"):
                    lead.name = result["name"]
                lead.email_verified = True
                lead.status = "enriched"

                # Update email record
                email.to_email = result["email"]
                email.enrichment_status = "enriched"

                # Deduct 1 credit
                _deduct_single_credit(db, email)

                db.commit()
                enriched_count += 1
                logger.info("[JIT-ENRICH] Enriched lead %d (%s) -> %s for email %d",
                            lead.id, lead.name, result["email"], email.id)
            else:
                # Enrichment returned no email
                lead.enrichment_fail_count += 1
                if lead.enrichment_fail_count >= MAX_ENRICHMENT_FAILURES:
                    email.enrichment_status = "skipped"
                    email.status = "failed"
                    email.error_message = f"Enrichment failed after {MAX_ENRICHMENT_FAILURES} attempts"
                    logger.warning("[JIT-ENRICH] Skipping lead %d (%s) after %d failures",
                                   lead.id, lead.name, lead.enrichment_fail_count)
                db.commit()

            time.sleep(0.2)  # Apollo rate limit

        except Exception as e:
            lead.enrichment_fail_count += 1
            if lead.enrichment_fail_count >= MAX_ENRICHMENT_FAILURES:
                email.enrichment_status = "skipped"
                email.status = "failed"
                email.error_message = f"Enrichment error: {str(e)[:200]}"
            db.commit()
            logger.error("[JIT-ENRICH] Error enriching lead %d: %s", lead.id, e)

    return enriched_count


def _deduct_single_credit(db, email: EmailSent):
    """Deduct 1 credit for a successful enrichment. Updates OutreachOrder tracking."""
    # Find the campaign's outreach order to get the user_id
    campaign = db.query(Campaign).filter_by(id=email.campaign_id).first()
    if not campaign or not campaign.candidate_id:
        return

    candidate = db.query(Candidate).filter_by(id=campaign.candidate_id).first()
    if not candidate:
        return

    user_id = candidate.user_id

    # Deduct from UserCredit
    credit = db.query(UserCredit).filter_by(user_id=user_id).first()
    if credit and (credit.total_credits - credit.used_credits) >= 1:
        credit.used_credits += 1
    else:
        logger.warning("[JIT-ENRICH] No credits available for user %s, skipping deduction", user_id)

    # Update OutreachOrder tracking
    order = (
        db.query(OutreachOrder)
        .filter_by(user_id=user_id, candidate_id=campaign.candidate_id)
        .filter(OutreachOrder.status.in_(["campaign_running", "email_connected", "campaign_setup"]))
        .order_by(OutreachOrder.created_at.desc())
        .first()
    )
    if order:
        order.credits_used = (order.credits_used or 0) + 1


# ── JIT Phase 2: Generate Email Content ─────────────────────────────────────

def _generate_pending(db) -> int:
    """Generate email content for enriched leads that don't have content yet.

    Returns number of emails successfully generated.
    """
    from services.email_campaign.email_generator_service import generate_email_for_lead

    now = datetime.utcnow()
    lookahead = now + timedelta(hours=JIT_LOOKAHEAD_HOURS)

    # Find enriched emails without content
    enriched_no_content = (
        db.query(EmailSent)
        .join(Campaign, EmailSent.campaign_id == Campaign.id)
        .filter(
            Campaign.status == "running",
            EmailSent.enrichment_status == "enriched",
            EmailSent.status == "pending_enrichment",
            EmailSent.subject.is_(None),
            EmailSent.scheduled_at.isnot(None),
            EmailSent.scheduled_at <= lookahead,
        )
        .order_by(EmailSent.scheduled_at.asc())
        .limit(MAX_GENERATE_PER_CYCLE)
        .all()
    )

    if not enriched_no_content:
        return 0

    generated_count = 0

    for email in enriched_no_content:
        lead = db.query(Lead).filter_by(id=email.lead_id).first()
        campaign = db.query(Campaign).filter_by(id=email.campaign_id).first()
        if not lead or not campaign:
            continue

        candidate = db.query(Candidate).filter_by(id=campaign.candidate_id).first()
        if not candidate:
            continue

        style = email.assigned_style or "warm_intro"

        try:
            subject, body = generate_email_for_lead(lead, candidate, style)
            email.subject = subject
            email.body = body
            email.status = "queued"  # Ready for Phase 3 (send)
            db.commit()
            generated_count += 1
            logger.info("[JIT-GENERATE] Generated email %d for lead %d (%s), style=%s",
                        email.id, lead.id, lead.name, style)

        except Exception as e:
            logger.error("[JIT-GENERATE] Failed to generate email %d for lead %d: %s",
                         email.id, lead.id, e)
            # Don't mark as failed — retry next cycle (within the 3-hour window)

    return generated_count


# ── JIT Phase 3: Send Ready Emails (unchanged logic) ────────────────────────

def _send_ready(db) -> tuple:
    """Find and send all emails where scheduled_at <= now and status = queued.

    Returns (sent_count, failed_count) tuple.
    """
    sent_count = 0
    failed_count = 0
    now = datetime.utcnow()

    # Find emails ready to send:
    # - Regular emails: only from running campaigns
    # - Test emails (is_test=True): from running, completed, or paused campaigns
    from sqlalchemy import or_, and_
    ready_emails = (
        db.query(EmailSent)
        .join(Campaign, EmailSent.campaign_id == Campaign.id)
        .filter(
            or_(
                and_(Campaign.status == "running", EmailSent.is_test == False),
                and_(Campaign.status.in_(["running", "completed", "paused"]), EmailSent.is_test == True),
            ),
            EmailSent.status == "queued",
            EmailSent.scheduled_at.isnot(None),
            EmailSent.scheduled_at <= now,
        )
        .order_by(EmailSent.scheduled_at.asc())
        .limit(MAX_SEND_PER_CYCLE)
        .with_for_update(skip_locked=True)
        .all()
    )

    if not ready_emails:
        return sent_count, failed_count

    logger.info("[SENDER] Found %d emails ready to send", len(ready_emails))

    # Group by campaign for token caching
    token_cache: dict = {}  # email_account_id -> access_token

    for email in ready_emails:
        campaign = db.query(Campaign).filter_by(id=email.campaign_id).first()
        if not campaign:
            continue
        # Regular emails: only from running campaigns. Test emails: also from completed/paused.
        if campaign.status != "running" and not email.is_test:
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

        # Lock this email so concurrent cycles cannot re-send it
        email.status = "sending"
        db.commit()

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
            email.thread_id = result.get("threadId")
            db.commit()
            sent_count += 1

            logger.info("[SENDER] Email %d sent successfully (gmail_id=%s, thread_id=%s)",
                        email.id, result.get("id"), result.get("threadId"))
            candidate = db.query(Candidate).filter_by(id=campaign.candidate_id).first()
            if candidate:
                ph_capture("email_sent", str(candidate.user_id), {
                    "campaign_id": email.campaign_id,
                    "assigned_style": email.assigned_style,
                    "is_jit_enriched": email.enrichment_status == "enriched",
                })

        except Exception as e:
            logger.error("[SENDER] Email %d failed: %s", email.id, e)
            email.status = "failed"
            email.error_message = str(e)[:500]
            db.commit()
            failed_count += 1
            candidate = db.query(Candidate).filter_by(id=campaign.candidate_id).first()
            if candidate:
                ph_capture("email_failed", str(candidate.user_id), {
                    "campaign_id": email.campaign_id,
                    "error_type": type(e).__name__,
                })

    return sent_count, failed_count


# ── Campaign Completion ──────────────────────────────────────────────────────

def _check_campaign_completion(db):
    """Mark campaigns as completed if no pending_enrichment or queued emails remain."""
    running_campaigns = db.query(Campaign).filter_by(status="running").all()
    for campaign in running_campaigns:
        # Flush any pending changes and expire cached attributes to get fresh counts
        db.flush()
        db.expire_all()

        remaining = db.query(func.count(EmailSent.id)).filter(
            EmailSent.campaign_id == campaign.id,
            EmailSent.status.in_(["pending_enrichment", "queued"]),
        ).scalar() or 0

        total = db.query(func.count(EmailSent.id)).filter(
            EmailSent.campaign_id == campaign.id,
        ).scalar() or 0

        sent = db.query(func.count(EmailSent.id)).filter(
            EmailSent.campaign_id == campaign.id,
            EmailSent.status == "sent",
        ).scalar() or 0

        if remaining == 0 and total > 0:
            campaign.status = "completed"
            db.commit()
            logger.info("[SENDER] Campaign %d completed — total=%d sent=%d remaining=%d",
                        campaign.id, total, sent, remaining)
        elif remaining > 0:
            logger.debug("[SENDER] Campaign %d still active — remaining=%d (total=%d sent=%d)",
                         campaign.id, remaining, total, sent)


# ── Credit Exhaustion Check ──────────────────────────────────────────────────

def _check_credit_exhaustion(db):
    """If a running campaign's user has no credits left, skip remaining pending emails."""
    running_campaigns = db.query(Campaign).filter_by(status="running").all()
    for campaign in running_campaigns:
        # Check if there are pending enrichment emails
        pending_count = db.query(func.count(EmailSent.id)).filter(
            EmailSent.campaign_id == campaign.id,
            EmailSent.enrichment_status == "pending",
        ).scalar() or 0

        if pending_count == 0:
            continue

        # Get user's credit balance
        candidate = db.query(Candidate).filter_by(id=campaign.candidate_id).first()
        if not candidate:
            continue

        credit = db.query(UserCredit).filter_by(user_id=candidate.user_id).first()
        if not credit:
            continue

        available = credit.total_credits - credit.used_credits
        if available <= 0:
            # Skip all remaining pending emails
            db.query(EmailSent).filter(
                EmailSent.campaign_id == campaign.id,
                EmailSent.enrichment_status == "pending",
            ).update({
                EmailSent.enrichment_status: "skipped",
                EmailSent.status: "failed",
                EmailSent.error_message: "Credits exhausted",
            }, synchronize_session="fetch")
            db.commit()
            logger.warning("[JIT] Credits exhausted for campaign %d (user %s), skipped %d emails",
                           campaign.id, candidate.user_id, pending_count)


# ── Reply & Bounce Check ─────────────────────────────────────────────────────

def _check_replies(db):
    """Phase 4: Check Gmail inbox for replies and bounces to sent outreach emails.

    Throttled to run every REPLY_CHECK_INTERVAL seconds (5 minutes).
    Only checks accounts that have running or recently completed campaigns.

    Returns:
        Dict with keys: replies_found, bounces_found.
    """
    global _last_reply_check

    now = time.time()
    if now - _last_reply_check < REPLY_CHECK_INTERVAL:
        return {"replies_found": 0, "bounces_found": 0}

    _last_reply_check = now
    replies_found = 0
    bounces_found = 0

    logger.info("[REPLY_CHECK] Starting reply check cycle")

    try:
        # Find all email accounts with running or completed campaigns
        running_account_ids = (
            db.query(Campaign.email_account_id)
            .filter(Campaign.status.in_(["running", "completed"]))
            .distinct()
            .all()
        )
        account_ids = [row[0] for row in running_account_ids if row[0]]

        if not account_ids:
            logger.debug("[REPLY_CHECK] No active campaigns — skipping")
            return {"replies_found": 0, "bounces_found": 0}

        for account_id in account_ids:
            account = db.query(EmailAccount).filter_by(id=account_id).first()
            if not account:
                continue

            try:
                # Refresh access token
                access_token = _refresh_token_sync(account, db)

                # Compute after_epoch: use last_reply_check_at or 24 hours ago
                if account.last_reply_check_at:
                    after_epoch = int(account.last_reply_check_at.timestamp())
                else:
                    after_epoch = int((datetime.utcnow() - timedelta(hours=24)).timestamp())

                # List new inbox messages since last check
                messages = list_inbox_messages(access_token, after_epoch)

                if not messages:
                    account.last_reply_check_at = datetime.utcnow()
                    db.commit()
                    continue

                # Get all thread_ids from sent emails for this account's campaigns
                campaign_ids = [
                    row[0] for row in
                    db.query(Campaign.id).filter_by(email_account_id=account_id).all()
                ]
                sent_emails_by_thread = {}
                if campaign_ids:
                    sent_emails = (
                        db.query(EmailSent)
                        .filter(
                            EmailSent.campaign_id.in_(campaign_ids),
                            EmailSent.thread_id.isnot(None),
                            EmailSent.status.in_(["sent", "replied"]),
                        )
                        .all()
                    )
                    sent_emails_by_thread = {e.thread_id: e for e in sent_emails}

                if not sent_emails_by_thread:
                    account.last_reply_check_at = datetime.utcnow()
                    db.commit()
                    continue

                # Process each inbox message
                for msg_stub in messages:
                    msg_thread_id = msg_stub.get("threadId")
                    if not msg_thread_id or msg_thread_id not in sent_emails_by_thread:
                        continue  # Not a reply to our outreach

                    email_row = sent_emails_by_thread[msg_thread_id]
                    if email_row.status == "bounced":
                        continue  # Already processed as bounce

                    detail = get_message_detail(access_token, msg_stub["id"])
                    if not detail:
                        continue

                    # Skip our own sent messages (same thread includes our outbound email)
                    if detail["from_email"] and account.email_address in detail["from_email"]:
                        continue

                    # Check for bounce
                    if is_bounce_message(detail["from_email"]):
                        email_row.status = "bounced"
                        email_row.bounce_reason = extract_bounce_reason(detail["body_text"])
                        db.commit()
                        bounces_found += 1
                        logger.info("[REPLY_CHECK] Bounce detected for email %d: %s",
                                    email_row.id, (email_row.bounce_reason or "")[:100])
                        continue

                    # First reply only — skip if already replied
                    if email_row.status == "replied":
                        continue

                    # Classify sentiment
                    classification = classify_reply_sentiment(detail["body_text"])
                    email_row.status = "replied"
                    email_row.reply_text = detail["body_text"][:10000]
                    email_row.reply_received_at = datetime.utcfromtimestamp(detail["internal_date"])
                    email_row.reply_sentiment = classification.get("sentiment", "neutral")
                    db.commit()
                    replies_found += 1
                    logger.info("[REPLY_CHECK] Reply detected for email %d: sentiment=%s",
                                email_row.id, email_row.reply_sentiment)

                # Update last check timestamp for this account
                account.last_reply_check_at = datetime.utcnow()
                db.commit()

            except Exception as e:
                logger.error("[REPLY_CHECK] Error checking account %d: %s", account_id, e, exc_info=True)
                continue

    except Exception as e:
        logger.error("[REPLY_CHECK] Reply check cycle failed: %s", e, exc_info=True)

    if replies_found > 0 or bounces_found > 0:
        logger.info("[REPLY_CHECK] Cycle complete — replies=%d bounces=%d", replies_found, bounces_found)

    return {"replies_found": replies_found, "bounces_found": bounces_found}


# ── Main Cycle ───────────────────────────────────────────────────────────────

def _process_cycle():
    """Run all JIT phases in sequence.

    Phase 1: Enrich upcoming leads (~0.6s)
    Phase 2: Generate email content (~2s)
    Phase 3: Send ready emails (~1s)
    Phase 4: Check replies/bounces (throttled to every 5 min)
    Total: ~3.6s worst case, under 5s k8s health check limit.

    Returns dict with counts from each phase.
    """
    db = SessionLocal()
    result = {"enriched": 0, "generated": 0, "sent": 0, "failed": 0, "replies": 0, "bounces": 0}
    try:
        # Phase 1: Enrich upcoming leads
        result["enriched"] = _enrich_upcoming(db)

        # Phase 2: Generate email content for enriched leads
        result["generated"] = _generate_pending(db)

        # Phase 3: Send ready emails
        sent, failed = _send_ready(db)
        result["sent"] = sent
        result["failed"] = failed

        # Post-cycle checks
        _check_campaign_completion(db)
        _check_credit_exhaustion(db)

        # Phase 4: Check for replies and bounces (throttled to every 5 min)
        reply_result = _check_replies(db)
        result["replies"] = reply_result.get("replies_found", 0)
        result["bounces"] = reply_result.get("bounces_found", 0)

    finally:
        db.close()

    if any(v > 0 for v in result.values()):
        logger.info("[CYCLE] enriched=%d generated=%d sent=%d failed=%d replies=%d bounces=%d",
                    result["enriched"], result["generated"], result["sent"], result["failed"],
                    result["replies"], result["bounces"])

    return result


# ── Legacy compat: keep old function name for worker endpoint ────────────────

def _process_ready_emails():
    """Legacy wrapper — calls _process_cycle() for backward compatibility."""
    cycle = _process_cycle()
    return cycle["sent"], cycle["failed"]


# ── Polling Sender Loop ──────────────────────────────────────────────────────

def _sender_loop():
    """Background thread: runs the 3-phase JIT cycle every POLL_INTERVAL seconds.

    Completely stateless — reads from DB, processes, updates DB. Safe across pod restarts.
    """
    logger.info("[SENDER] JIT email sender loop started (poll every %ds)", POLL_INTERVAL)

    while not _sender_stop.is_set():
        try:
            _process_cycle()
        except Exception as e:
            logger.error("[SENDER] Error in sender loop: %s", e, exc_info=True)

        # Sleep in small chunks so we can stop quickly
        for _ in range(POLL_INTERVAL):
            if _sender_stop.is_set():
                break
            time.sleep(1)

    logger.info("[SENDER] Email sender loop stopped")


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
