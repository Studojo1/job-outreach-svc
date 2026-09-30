"""Reply rate of each campaign's first 100 emails, by launch week (audit PS-N03).

Replies per email sent fell from about 4% in May to under 1% in September,
but mostly because later sends are the low-ranked tail of old campaigns:
leads go out best-score first, so emails 1-100 of a campaign reply at about
3% and 201+ at about 1.2%. A monthly "replies / sent" line mixes the two and
cannot tell a real decline from a change in mix. The like-for-like number is
the reply rate of each campaign's first 100 first-touch emails, grouped by
the week the campaign launched.

Served by GET /admin/outreach/reply-rate/first-100-by-launch-week. The hourly
data-health check fresh_campaigns_low_reply_rate (emailer-service) pages on
the same definition.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.models import Campaign, EmailSent

FIRST_N = 100
ALERT_BELOW_PCT = 1.5   # the founders' alert line
MIN_AGE_DAYS = 10       # replies need time to arrive; younger emails are not counted
MIN_EMAILS_FOR_ALERT = 50  # a week with fewer counted emails is too thin to alarm on


def _week_start(ts: datetime) -> datetime:
    day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())  # Monday


def first_100_reply_rate_by_launch_week(
    db: Session,
    *,
    weeks: int = 12,
    now: Optional[datetime] = None,
    min_age_days: int = MIN_AGE_DAYS,
) -> Dict[str, Any]:
    """Per launch week: campaigns, emails counted, replies, reply_rate_pct and
    below_alert. Only a campaign's first FIRST_N first-touch emails (by send
    time, tests excluded) count, and of those only the ones sent at least
    min_age_days ago."""
    now = now or datetime.utcnow()
    since = _week_start(now) - timedelta(weeks=max(1, weeks) - 1)
    age_cutoff = now - timedelta(days=min_age_days)
    launched = func.coalesce(Campaign.started_at, Campaign.created_at)

    ranked = (
        db.query(
            EmailSent.campaign_id.label("campaign_id"),
            EmailSent.sent_at.label("sent_at"),
            or_(EmailSent.status == "replied", EmailSent.reply_received_at.isnot(None)).label("replied"),
            func.row_number().over(
                partition_by=EmailSent.campaign_id,
                order_by=(EmailSent.sent_at.asc(), EmailSent.id.asc()),
            ).label("n"),
        )
        .join(Campaign, Campaign.id == EmailSent.campaign_id)
        .filter(
            launched >= since,
            func.coalesce(EmailSent.followup_number, 0) == 0,
            EmailSent.is_test.isnot(True),
            EmailSent.sent_at.isnot(None),
        )
        .subquery()
    )
    rows = (
        db.query(ranked.c.campaign_id, ranked.c.sent_at, ranked.c.replied)
        .filter(ranked.c.n <= FIRST_N, ranked.c.sent_at <= age_cutoff)
        .all()
    )

    per_campaign: Dict[int, List[int]] = {}
    for campaign_id, _sent_at, replied in rows:
        tally = per_campaign.setdefault(campaign_id, [0, 0])
        tally[0] += 1
        tally[1] += 1 if replied else 0

    launch_of = dict(
        db.query(Campaign.id, launched).filter(Campaign.id.in_(list(per_campaign) or [-1])).all()
    )
    by_week: Dict[datetime, Dict[str, Any]] = {}
    for campaign_id, (emails, replies) in per_campaign.items():
        week = _week_start(launch_of[campaign_id])
        w = by_week.setdefault(week, {"campaigns": [], "emails": 0, "replies": 0})
        w["campaigns"].append({"campaign_id": campaign_id, "emails": emails, "replies": replies})
        w["emails"] += emails
        w["replies"] += replies

    out = []
    for week in sorted(by_week):
        w = by_week[week]
        rate = round(w["replies"] / w["emails"] * 100, 2) if w["emails"] else None
        out.append({
            "week_start": week.date().isoformat(),
            "campaigns": len(w["campaigns"]),
            "emails": w["emails"],
            "replies": w["replies"],
            "reply_rate_pct": rate,
            "below_alert": rate is not None and w["emails"] >= MIN_EMAILS_FOR_ALERT and rate < ALERT_BELOW_PCT,
            "by_campaign": sorted(w["campaigns"], key=lambda c: c["campaign_id"]),
        })
    return {
        "first_n": FIRST_N,
        "min_age_days": min_age_days,
        "alert_below_pct": ALERT_BELOW_PCT,
        "weeks": out,
    }
