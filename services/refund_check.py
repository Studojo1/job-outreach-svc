"""Refund Policy v3.0 §3.3: has an outreach campaign failed completely?

A campaign qualifies for a refund of its unsent credits only if all six hold:
  1. it was active (not paused, cancelled or finished) through the streak;
  2. Gmail stayed connected through the streak;
  3. it sent no emails for 7 consecutive days;
  4. the cause was Studojo's own systems (a person decides; we show evidence);
  5. the user reported it within 15 days of the last email it sent;
  6. we could not get it sending again within 7 days of that report.

The admin panel's campaign refund-check page shows this, and the partial
refund endpoint recomputes it server-side before moving any money, so the page
and the refund can never disagree.

"Sent" means a first-touch email Gmail accepted (sent_at set, not a test).
Follow-ups and bounce replacements are free, so they don't count towards the
credits used, but any send at all breaks a zero-send streak.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from database.models import Campaign, CampaignNotice, Candidate, EmailSent, PaymentOrder, SystemEvent, User

STREAK_DAYS = 7          # §3.3 condition 3
REPORT_WINDOW_DAYS = 15  # §3.3 condition 5
FIX_WINDOW_DAYS = 7      # §3.3 condition 6

YES, NO, CHECK, OPEN = "yes", "no", "check", "open"


class RefundCheckError(Exception):
    pass


def _local(dt: datetime | None, tz: ZoneInfo) -> date | None:
    if dt is None:
        return None
    # Stored timestamps are naive UTC.
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _payment_for(db: Session, campaign: Campaign) -> PaymentOrder | None:
    if not campaign.outreach_order_id:
        return None
    return (
        db.query(PaymentOrder)
        .filter(PaymentOrder.outreach_order_id == campaign.outreach_order_id,
                PaymentOrder.status.in_(("paid", "completed", "refunded")))
        .order_by(PaymentOrder.created_at.desc())
        .first()
    )


def campaign_refund_check(db: Session, campaign_id: int, *, reported_on: date | None = None,
                          now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise RefundCheckError("Campaign not found.")
    tz = ZoneInfo(campaign.user_timezone or "Asia/Kolkata")
    owner = (
        db.query(User.id, User.email)
        .join(Candidate, Candidate.user_id == User.id)
        .filter(Candidate.id == campaign.candidate_id)
        .first()
    )

    start = _local(campaign.started_at or campaign.created_at, tz)
    end = _local(campaign.completed_at, tz) if campaign.status in ("completed", "cancelled") and campaign.completed_at else _local(now, tz)
    today = _local(now, tz)
    if start is None or end is None or end < start:
        end = start = today

    rows = (
        db.query(EmailSent.sent_at, EmailSent.followup_number)
        .filter(EmailSent.campaign_id == campaign.id, EmailSent.sent_at.isnot(None),
                EmailSent.is_test.isnot(True))
        .all()
    )
    per_day: dict[date, int] = {}
    first_touch_sent = 0
    for sent_at, followup in rows:
        d = _local(sent_at, tz)
        per_day[d] = per_day.get(d, 0) + 1
        if not followup:
            first_touch_sent += 1
    days = _days(start, end)
    daily = [{"date": d.isoformat(), "sent": per_day.get(d, 0)} for d in days]

    # Longest run of zero-send days inside the campaign's life.
    best: tuple[date, date] | None = None
    run_start = None
    for d in days + [None]:
        if d is not None and per_day.get(d, 0) == 0:
            run_start = run_start or d
            continue
        if run_start is not None:
            run_end = (d - timedelta(days=1)) if d else days[-1]
            if best is None or (run_end - run_start) > (best[1] - best[0]):
                best = (run_start, run_end)
            run_start = None
    streak_len = (best[1] - best[0]).days + 1 if best else 0

    # Pauses. There is no resume history, so a pause inside the streak makes
    # condition 1 a judgement call rather than a clean yes.
    pauses = []
    for ev in db.query(SystemEvent).filter(SystemEvent.event_type == "campaign_paused").all():
        meta = ev.meta or {}
        if meta.get("campaign_id") == campaign.id:
            pauses.append({"date": _local(ev.created_at, tz).isoformat(), "reason": meta.get("pause_reason"),
                           "by": meta.get("paused_by")})
    if campaign.status == "paused" and campaign.paused_at:
        cur = {"date": _local(campaign.paused_at, tz).isoformat(), "reason": campaign.pause_reason,
               "by": campaign.paused_by, "current": True}
        if not any(p["date"] == cur["date"] and p["reason"] == cur["reason"] for p in pauses):
            pauses.append(cur)
    pauses.sort(key=lambda p: p["date"])

    def in_streak(iso: str) -> bool:
        return bool(best) and best[0].isoformat() <= iso <= best[1].isoformat()

    streak_pauses = [p for p in pauses if in_streak(p["date"])]
    # A pause that started before the streak and is still in force covers it too.
    if best and campaign.status == "paused" and campaign.paused_at and _local(campaign.paused_at, tz) <= best[1]:
        streak_pauses = streak_pauses or [p for p in pauses if p.get("current")]

    gmail_notices = [
        _local(n.created_at, tz).isoformat()
        for n in db.query(CampaignNotice).filter(CampaignNotice.campaign_id == campaign.id).all()
        if "gmail" in (n.kind or "")
    ]
    gmail_problems = [p for p in streak_pauses if p["reason"] == "gmail_auth"] + [
        {"date": d, "reason": "gmail reconnect email sent"} for d in gmail_notices if in_streak(d)]

    # Evidence for condition 4: failures logged during the streak.
    failures: dict[str, int] = {}
    if best:
        lo = datetime.combine(best[0], datetime.min.time(), tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        hi = datetime.combine(best[1] + timedelta(days=1), datetime.min.time(), tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        for status, err in (
            db.query(EmailSent.status, EmailSent.error_message)
            .filter(EmailSent.campaign_id == campaign.id,
                    EmailSent.status.in_(("failed", "generation_failed")),
                    EmailSent.status_changed_at >= lo, EmailSent.status_changed_at < hi)
            .all()
        ):
            key = f"{status}: {(err or 'no message').splitlines()[0][:90]}"
            failures[key] = failures.get(key, 0) + 1
    evidence = [{"what": k, "count": v} for k, v in sorted(failures.items(), key=lambda kv: -kv[1])[:5]]
    evidence += [{"what": f"paused by {p.get('by') or 'system'}: {p['reason']}", "count": 1}
                 for p in streak_pauses if p.get("by") != "user"]

    # Conditions.
    c1 = YES if best and not streak_pauses and campaign.status != "draft" else (CHECK if best else NO)
    c2 = YES if best and not gmail_problems else (NO if gmail_problems else CHECK)
    c3 = YES if streak_len >= STREAK_DAYS else NO
    c4 = CHECK

    last_send_before = None
    if best:
        prior = [d for d in per_day if d < best[0]]
        last_send_before = max(prior) if prior else None
    anchor = last_send_before or start
    if reported_on is None:
        c5 = OPEN
        c6 = OPEN
        fix_note = "Enter the date the user reported it"
    else:
        c5 = YES if (reported_on - anchor).days <= REPORT_WINDOW_DAYS else NO
        window = [reported_on + timedelta(days=i) for i in range(1, FIX_WINDOW_DAYS + 1)]
        resumed = sum(per_day.get(d, 0) for d in window if d <= today)
        deadline = reported_on + timedelta(days=FIX_WINDOW_DAYS)
        if resumed > 0:
            c6, fix_note = NO, f"sending resumed ({resumed} sent) after the report"
        elif today >= deadline:
            c6, fix_note = YES, f"no sends in the 7 days to {deadline.isoformat()}"
        else:
            c6, fix_note = OPEN, f"day {(today - reported_on).days} of {FIX_WINDOW_DAYS}"

    # Money. Value each credit at what was actually paid for the pack.
    reserved = campaign.credits_reserved or 0
    unsent = max(reserved - first_touch_sent, 0)
    payment = _payment_for(db, campaign)
    refund = None
    if payment and payment.amount_cents and (payment.credits_granted or payment.tier):
        credits_bought = payment.credits_granted or payment.tier
        per_credit = payment.amount_cents / credits_bought
        remaining = payment.amount_cents - (payment.refunded_cents or 0)
        amount = min(round(unsent * per_credit), remaining)
        refund = {
            "payment_id": payment.id, "provider": payment.provider, "currency": payment.currency,
            "paid_cents": payment.amount_cents, "already_refunded_cents": payment.refunded_cents or 0,
            "credits_bought": credits_bought, "per_credit_cents": round(per_credit, 2),
            "unsent_credits": unsent, "amount_cents": max(amount, 0),
            "provider_payment_id": payment.razorpay_payment_id or payment.dodo_payment_id,
        }

    conditions = [
        {"n": 1, "label": "Campaign active", "result": c1,
         "note": "no pauses in the streak" if c1 == YES else (f"{len(streak_pauses)} pause(s) in the streak" if best else "no zero-send streak")},
        {"n": 2, "label": "Gmail connected", "result": c2,
         "note": "no Gmail disconnection in the streak" if c2 == YES else "; ".join(f"{g['date']} {g['reason']}" for g in gmail_problems)},
        {"n": 3, "label": f"Zero sends for {STREAK_DAYS} consecutive days", "result": c3, "note": f"{streak_len} days"},
        {"n": 4, "label": "Our fault", "result": c4,
         "note": "; ".join(f"{e['what']} ×{e['count']}" for e in evidence) or "no failures logged: check worker logs"},
        {"n": 5, "label": f"Reported within {REPORT_WINDOW_DAYS} days of last send ({anchor.isoformat()})", "result": c5,
         "note": f"reported {reported_on.isoformat()}" if reported_on else "Enter the date the user reported it"},
        {"n": 6, "label": f"Not fixed within {FIX_WINDOW_DAYS} days of report", "result": c6, "note": fix_note},
    ]
    automatic = [c["result"] for c in conditions if c["n"] != 4]
    if all(r == YES for r in automatic):
        verdict = "met"      # a person still confirms condition 4
    elif any(r == NO for r in automatic):
        verdict = "not_met"
    else:
        verdict = "open"

    return {
        "campaign": {
            "id": campaign.id, "name": campaign.name, "status": campaign.status,
            "pause_reason": campaign.pause_reason, "timezone": str(tz),
            "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
            "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
            "user_id": owner.id if owner else None, "user_email": owner.email if owner else None,
            "credits_reserved": reserved, "first_touch_sent": first_touch_sent,
        },
        "daily": daily,
        "streak": {"days": streak_len, "start": best[0].isoformat() if best else None,
                   "end": best[1].isoformat() if best else None},
        "gmail": {"problems": gmail_problems},
        "pauses": pauses,
        "conditions": conditions,
        "verdict": verdict,
        "refund": refund,
    }
