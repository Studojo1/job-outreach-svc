"""Admin endpoints for outreach order monitoring dashboard."""

import copy
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select as sa_select, text
from sqlalchemy.orm import Session

from api.dependencies import get_admin_user
from database.models import (
    Campaign,
    Candidate,
    Coupon,
    EmailAccount,
    EmailSent,
    Lead,
    LeadScore,
    OutreachOrder,
    PaymentOrder,
    User,
    UserCredit,
)
from database.session import get_db

router = APIRouter(prefix="/admin/outreach", tags=["admin"])


# Linear main-flow stages — every user passes through these in order. Used
# for monotonic funnel counting (a user counted at stage N is also counted
# at every stage 0..N).
MAIN_FLOW_STAGES = [
    ("resume_uploaded",       "Resume Uploaded",       "resume_uploaded_at"),
    ("quiz_completed",        "Quiz Completed",        "quiz_completed_at"),
    ("leads_generated",       "Leads Generated",       "leads_generated_at"),
    ("payment_page_reached",  "Payment Page Reached",  "payment_page_reached_at"),
    ("payment_made",          "Payment Made",          "payment_made_at"),
    ("gmail_connected",       "Gmail Connected",       "gmail_connected_at"),
    ("email_style_selected",  "Email Style Selected",  "email_style_selected_at"),
    ("campaign_setup",        "Campaign Setup",        "campaign_setup_at"),
    ("campaign_launched",     "Campaign Launched",     "campaign_launched_at"),
    ("campaign_completed",    "Campaign Completed",    "campaign_completed_at"),
]
# Side-branches that don't fit the linear waterfall. Counted independently.
SIDE_STAGES = [
    ("campaign_paused",       "Campaign Paused",       "campaign_paused_at"),
]
# Combined list (display order) — used for the per-user dot strip.
FUNNEL_STAGES = MAIN_FLOW_STAGES[:9] + SIDE_STAGES + MAIN_FLOW_STAGES[9:]


@router.get("/recent-signups")
def recent_signups(
    days: int = Query(2, ge=1, le=30),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Return all users who signed up in the last N days (default 2)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    users = (
        db.query(User)
        .filter(User.created_at >= cutoff)
        .order_by(User.created_at.desc())
        .all()
    )
    return [
        {
            "email": u.email,
            "name": u.name,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "role": "candidate",
        }
        for u in users
    ]

STUCK_THRESHOLD_HOURS = 6
ACTIVE_STATUSES = [
    "profile_complete", "leads_generating", "leads_ready", "enriching",
    "enrichment_complete", "campaign_setup", "email_connected", "campaign_running",
]
# Only flag stuck for states where the system is doing automated work.
# User-action states (leads_ready, enrichment_complete, campaign_setup,
# email_connected) wait for user input and should never be marked stuck.
SYSTEM_PROCESSING_STATUSES = [
    "leads_generating",
    "enriching",
    "campaign_running",
]


def _meaningful_ts(order, fallback_dt) -> str | None:
    """Return the most meaningful 'last active' timestamp for an order.

    If updated_at ≈ created_at (within 2s) the order was never genuinely
    progressed by user action (e.g. batch-backfilled or just created).
    In that case fall back to the user's own created_at so the admin panel
    shows when the person actually signed up rather than a batch-op timestamp.
    """
    ts = order.updated_at or order.created_at
    if ts is None:
        return fallback_dt.isoformat() if fallback_dt else None
    created = order.created_at
    if created:
        delta = abs((ts - created).total_seconds())
        if delta < 2:
            ts = fallback_dt or ts
    return ts.isoformat() if ts else None


@router.get("/overview")
async def outreach_overview(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Dashboard-level aggregate stats across all outreach orders."""
    now = datetime.now(timezone.utc)
    stuck_cutoff = now - timedelta(hours=STUCK_THRESHOLD_HOURS)

    # Order counts by status
    status_counts = (
        db.query(OutreachOrder.status, func.count())
        .group_by(OutreachOrder.status)
        .all()
    )
    orders_by_status = {s: c for s, c in status_counts}
    total_orders = sum(orders_by_status.values())
    active_orders = sum(orders_by_status.get(s, 0) for s in ACTIVE_STATUSES)
    completed_orders = orders_by_status.get("completed", 0)

    # Stuck orders (only system-processing states — not user-action states)
    stuck_orders = (
        db.query(func.count())
        .select_from(OutreachOrder)
        .filter(
            OutreachOrder.status.in_(SYSTEM_PROCESSING_STATUSES),
            OutreachOrder.updated_at < stuck_cutoff,
        )
        .scalar()
    ) or 0

    # Paid payment orders
    paid_orders = (
        db.query(func.count())
        .select_from(PaymentOrder)
        .filter(PaymentOrder.status == "paid")
        .scalar()
    ) or 0

    # Revenue
    total_revenue_cents = (
        db.query(func.coalesce(func.sum(PaymentOrder.amount_cents), 0))
        .filter(PaymentOrder.status == "paid")
        .scalar()
    ) or 0

    # Email stats — raw counts kept for the Email Trend chart (volume metric)
    email_stats = (
        db.query(EmailSent.status, func.count())
        .group_by(EmailSent.status)
        .all()
    )
    email_map = {s: c for s, c in email_stats}
    total_sent = email_map.get("sent", 0) + email_map.get("replied", 0)
    total_replied = email_map.get("replied", 0)
    total_bounced = email_map.get("bounced", 0)

    # Opened is orthogonal to status: a sent/replied email may also be opened.
    # Count rows with at least one pixel load. Approximate (mail-client image
    # prefetch inflates); shown as "Open rate (approx)" in the dashboard.
    total_opened = (
        db.query(func.count())
        .select_from(EmailSent)
        .filter(EmailSent.open_count > 0)
        .scalar()
    ) or 0
    open_rate_pct = round((total_opened / total_sent * 100), 1) if total_sent > 0 else 0.0

    # Correct reply rate: unique leads replied / unique leads contacted (initial only,
    # paid non-internal campaigns, excl bounced from denominator)
    _STATS_EXCL = {
        "businessconnect.pranav@gmail.com",
        "pranav.hegde126@gmail.com",
        "pranavshastry4@gmail.com",
        "studojo@gmail.com",
        "benjenstark.578239@gmail.com",
        "pranavs.hegde@bbafeh.christuniversity.in",
    }
    leads_contacted_total = (
        db.query(func.count(func.distinct(EmailSent.lead_id)))
        .select_from(EmailSent)
        .join(OutreachOrder, OutreachOrder.campaign_id == EmailSent.campaign_id)
        .join(User, User.id == OutreachOrder.user_id)
        .join(PaymentOrder, PaymentOrder.user_id == User.id)
        .filter(
            EmailSent.followup_number == 0,
            EmailSent.status.in_(["sent", "replied"]),
            ~EmailSent.is_test,
            EmailSent.lead_id.isnot(None),
            PaymentOrder.status == "paid",
            PaymentOrder.amount_cents > 0,
            User.email.notin_(_STATS_EXCL),
        )
        .scalar()
    ) or 0
    leads_replied_total = (
        db.query(func.count(func.distinct(EmailSent.lead_id)))
        .select_from(EmailSent)
        .join(OutreachOrder, OutreachOrder.campaign_id == EmailSent.campaign_id)
        .join(User, User.id == OutreachOrder.user_id)
        .join(PaymentOrder, PaymentOrder.user_id == User.id)
        .filter(
            EmailSent.reply_received_at.isnot(None),
            ~EmailSent.is_test,
            EmailSent.lead_id.isnot(None),
            PaymentOrder.status == "paid",
            PaymentOrder.amount_cents > 0,
            User.email.notin_(_STATS_EXCL),
        )
        .scalar()
    ) or 0
    reply_rate_pct = round((leads_replied_total / leads_contacted_total * 100), 1) if leads_contacted_total > 0 else 0.0

    # Monthly metrics (last 12 months)
    twelve_months_ago = now - timedelta(days=365)

    monthly_orders = (
        db.query(
            func.to_char(OutreachOrder.created_at, "YYYY-MM").label("month"),
            func.count().label("orders_created"),
        )
        .filter(OutreachOrder.created_at >= twelve_months_ago)
        .group_by("month")
        .all()
    )

    monthly_revenue = (
        db.query(
            func.to_char(PaymentOrder.created_at, "YYYY-MM").label("month"),
            func.coalesce(func.sum(PaymentOrder.amount_cents), 0).label("revenue_cents"),
        )
        .filter(PaymentOrder.status == "paid", PaymentOrder.created_at >= twelve_months_ago)
        .group_by("month")
        .all()
    )

    monthly_emails = (
        db.query(
            func.to_char(EmailSent.sent_at, "YYYY-MM").label("month"),
            func.count().label("emails_sent"),
            func.sum(case((EmailSent.status == "replied", 1), else_=0)).label("emails_replied"),
            func.sum(case((EmailSent.open_count > 0, 1), else_=0)).label("emails_opened"),
        )
        .filter(EmailSent.sent_at.isnot(None), EmailSent.sent_at >= twelve_months_ago)
        .group_by("month")
        .all()
    )

    # Merge monthly data
    months: dict = {}
    for row in monthly_orders:
        months.setdefault(row.month, {})["orders_created"] = row.orders_created
    for row in monthly_revenue:
        months.setdefault(row.month, {})["revenue_cents"] = int(row.revenue_cents)
    for row in monthly_emails:
        m = months.setdefault(row.month, {})
        m["emails_sent"] = row.emails_sent
        m["emails_replied"] = int(row.emails_replied or 0)
        m["emails_opened"] = int(row.emails_opened or 0)

    monthly_metrics = sorted(
        [
            {
                "month": m,
                "orders_created": d.get("orders_created", 0),
                "revenue_cents": d.get("revenue_cents", 0),
                "emails_sent": d.get("emails_sent", 0),
                "emails_replied": d.get("emails_replied", 0),
                "emails_opened": d.get("emails_opened", 0),
            }
            for m, d in months.items()
        ],
        key=lambda x: x["month"],
    )

    # ── Funnel aggregate ────────────────────────────────────────────────────
    # MONOTONIC counting: a user is counted at stage N if they have a timestamp
    # for stage N OR any later main-flow stage. This guarantees the chart is
    # provably non-increasing (you can't have more users at a later stage than
    # at an earlier one), and corrects for backfill misses where a downstream
    # timestamp is set but an upstream one isn't.
    from sqlalchemy import or_
    funnel = []
    prev_count: int | None = None
    for i, (key, label, column) in enumerate(MAIN_FLOW_STAGES):
        # "this stage or any later" — covers backfill gaps
        cols_or_later = [getattr(OutreachOrder, c) for _, _, c in MAIN_FLOW_STAGES[i:]]
        cond = or_(*[c.isnot(None) for c in cols_or_later])
        users_reached = (
            db.query(func.count(func.distinct(OutreachOrder.user_id)))
            .filter(cond)
            .scalar()
        ) or 0
        drop_off = None
        drop_off_pct = None
        if prev_count is not None and prev_count > 0:
            drop_off = max(prev_count - users_reached, 0)
            drop_off_pct = round((drop_off / prev_count) * 100, 1)
        entry = {
            "stage": key,
            "label": label,
            "users_reached": users_reached,
            "drop_off_from_prev": drop_off,
            "drop_off_pct_from_prev": drop_off_pct,
        }
        # Insert paused as a side-branch right before campaign_completed so
        # the chart visually separates terminal off-ramps.
        funnel.append(entry)
        if key == "campaign_launched":
            paused_col = getattr(OutreachOrder, "campaign_paused_at")
            paused_count = (
                db.query(func.count(func.distinct(OutreachOrder.user_id)))
                .filter(paused_col.isnot(None))
                .scalar()
            ) or 0
            funnel.append({
                "stage": "campaign_paused",
                "label": "Campaign Paused",
                "users_reached": paused_count,
                "drop_off_from_prev": None,
                "drop_off_pct_from_prev": None,
            })
        prev_count = users_reached

    # Period reply rates — cohort by month of first contact.
    # Buckets each unique lead by when they first received an initial email,
    # then checks whether they ever replied (any email in the thread).
    _first_contact_sq = (
        db.query(
            EmailSent.lead_id.label("lead_id"),
            func.to_char(func.min(EmailSent.sent_at), "YYYY-MM").label("cohort_month"),
        )
        .select_from(EmailSent)
        .join(OutreachOrder, OutreachOrder.campaign_id == EmailSent.campaign_id)
        .join(User, User.id == OutreachOrder.user_id)
        .join(PaymentOrder, PaymentOrder.user_id == User.id)
        .filter(
            EmailSent.followup_number == 0,
            EmailSent.status.in_(["sent", "replied"]),
            ~EmailSent.is_test,
            EmailSent.lead_id.isnot(None),
            EmailSent.sent_at.isnot(None),
            PaymentOrder.status == "paid",
            PaymentOrder.amount_cents > 0,
            User.email.notin_(_STATS_EXCL),
        )
        .group_by(EmailSent.lead_id)
        .subquery()
    )
    _any_reply_sq = (
        db.query(EmailSent.lead_id.label("lead_id"))
        .filter(EmailSent.reply_received_at.isnot(None), EmailSent.lead_id.isnot(None))
        .distinct()
        .subquery()
    )
    _period_rows = (
        db.query(
            _first_contact_sq.c.cohort_month,
            func.count(_first_contact_sq.c.lead_id).label("leads_contacted"),
            func.count(_any_reply_sq.c.lead_id).label("leads_replied"),
        )
        .outerjoin(_any_reply_sq, _any_reply_sq.c.lead_id == _first_contact_sq.c.lead_id)
        .group_by(_first_contact_sq.c.cohort_month)
        .order_by(_first_contact_sq.c.cohort_month)
        .all()
    )
    period_reply_rates = [
        {
            "month": row.cohort_month,
            "leads_contacted": row.leads_contacted,
            "leads_replied": row.leads_replied,
            "reply_rate": round(row.leads_replied / row.leads_contacted * 100, 1) if row.leads_contacted > 0 else 0.0,
        }
        for row in _period_rows
    ]

    return {
        "total_orders": total_orders,
        "paid_orders": paid_orders,
        "active_orders": active_orders,
        "completed_orders": completed_orders,
        "stuck_orders": stuck_orders,
        "total_revenue_cents": total_revenue_cents,
        "total_emails_sent": total_sent,
        "total_emails_replied": total_replied,
        "total_emails_bounced": total_bounced,
        "total_emails_opened": total_opened,
        "reply_rate_pct": reply_rate_pct,
        "open_rate_pct": open_rate_pct,
        "leads_contacted": leads_contacted_total,
        "leads_replied": leads_replied_total,
        "period_reply_rates": period_reply_rates,
        "orders_by_status": orders_by_status,
        "monthly_metrics": monthly_metrics,
        "funnel": funnel,
    }


@router.get("/users")
async def outreach_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query("", max_length=200),
    status_filter: str = Query("", max_length=50),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all users with outreach orders and per-user aggregated stats."""
    now = datetime.now(timezone.utc)
    stuck_cutoff = now - timedelta(hours=STUCK_THRESHOLD_HOURS)

    # Base: users who have at least one outreach order
    user_ids_q = db.query(OutreachOrder.user_id).distinct()
    if status_filter:
        user_ids_q = user_ids_q.filter(OutreachOrder.status == status_filter)
    user_ids = [r[0] for r in user_ids_q.all()]

    if not user_ids:
        return {"users": [], "total": 0}

    # Filter by search
    users_q = db.query(User).filter(User.id.in_(user_ids))
    if search:
        pattern = f"%{search}%"
        users_q = users_q.filter(
            (User.name.ilike(pattern)) | (User.email.ilike(pattern))
        )

    total = users_q.count()
    # Correlated subquery: max updated_at across all orders per user.
    # Secondary sort by User.id ensures deterministic pagination even when
    # multiple users share the same updated_at (e.g. batch-backfilled orders).
    latest_update_sq = (
        sa_select(func.max(OutreachOrder.updated_at))
        .where(OutreachOrder.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    users = (
        users_q
        .order_by(latest_update_sq.desc().nullslast(), User.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    result = []
    for u in users:
        # Orders — latest order for display, active order for stuck detection
        orders = db.query(OutreachOrder).filter(OutreachOrder.user_id == u.id).all()
        active_order = next((o for o in orders if o.status in ACTIVE_STATUSES), None)
        latest_order = max(orders, key=lambda o: o.updated_at or o.created_at, default=None) if orders else None

        # Currently-running campaign for this user. Used to override the
        # active_campaign_id below, because a user can have a paused dupe
        # campaign whose order is more recent than the real running campaign
        # (legacy state from before the Apr 22 dupe-block fix).
        running_campaign = (
            db.query(Campaign)
            .join(OutreachOrder, OutreachOrder.candidate_id == Campaign.candidate_id)
            .filter(OutreachOrder.user_id == u.id, Campaign.status == "running")
            .order_by(Campaign.created_at.desc())
            .first()
        )

        # Payment
        total_paid = (
            db.query(func.coalesce(func.sum(PaymentOrder.amount_cents), 0))
            .filter(PaymentOrder.user_id == u.id, PaymentOrder.status == "paid")
            .scalar()
        ) or 0

        # Credits
        credit = db.query(UserCredit).filter(UserCredit.user_id == u.id).first()

        # Email stats via campaigns linked to orders
        campaign_ids = [o.campaign_id for o in orders if o.campaign_id]
        emails_sent = emails_replied = emails_bounced = 0
        if campaign_ids:
            e_stats = (
                db.query(
                    EmailSent.status,
                    func.count(),
                )
                .filter(EmailSent.campaign_id.in_(campaign_ids))
                .group_by(EmailSent.status)
                .all()
            )
            e_map = {s: c for s, c in e_stats}
            emails_sent = e_map.get("sent", 0) + e_map.get("replied", 0)
            emails_replied = e_map.get("replied", 0)
            emails_bounced = e_map.get("bounced", 0)

        # Total leads
        candidate_ids = [o.candidate_id for o in orders if o.candidate_id]
        total_leads = 0
        if candidate_ids:
            total_leads = (
                db.query(func.count())
                .select_from(Lead)
                .filter(Lead.candidate_id.in_(candidate_ids))
                .scalar()
            ) or 0

        is_stuck = (
            active_order is not None
            and active_order.status in SYSTEM_PROCESSING_STATUSES
            and active_order.updated_at
            and active_order.updated_at.replace(tzinfo=timezone.utc) < stuck_cutoff
        )

        # Funnel: collapse stage timestamps across all of a user's orders
        # into a single per-stage earliest-reached timestamp. This is what
        # the admin panel uses to render the per-user journey timeline.
        stage_ts = {}
        for _key, _label, _column in FUNNEL_STAGES:
            best = None
            for o in orders:
                v = getattr(o, _column, None)
                if v is not None and (best is None or v < best):
                    best = v
            stage_ts[_key] = best  # store as datetime, serialize below

        # Current stage = the latest main-flow stage the user has reached.
        # We walk MAIN_FLOW_STAGES in reverse and pick the first stage that
        # has a timestamp. This is what the Status column displays — much
        # more informative than the raw OutreachOrder.status, which doesn't
        # capture pre-order stages (resume_uploaded, quiz_completed, etc.).
        current_stage = None
        current_stage_label = None
        for _k, _l, _c in reversed(MAIN_FLOW_STAGES):
            if stage_ts.get(_k) is not None:
                current_stage = _k
                current_stage_label = _l
                break

        # Now serialize stage_ts to ISO strings
        stage_ts = {k: (v.isoformat() if v else None) for k, v in stage_ts.items()}

        result.append({
            "user_id": u.id,
            "user_name": u.name,
            "user_email": u.email,
            "total_orders": len(orders),
            "stage_timestamps": stage_ts,
            "current_stage": current_stage,
            "current_stage_label": current_stage_label,
            # Prefer the running campaign's status/id over whatever the latest
            # order points at — handles legacy users with a paused dupe order
            # whose updated_at is newer than the real running campaign's order.
            "active_order_status": (
                "campaign_running" if running_campaign else (latest_order.status if latest_order else None)
            ),
            "active_order_id": latest_order.id if latest_order else None,
            "active_campaign_id": (
                running_campaign.id if running_campaign else next(
                    (o.campaign_id for o in sorted(orders, key=lambda o: o.updated_at or o.created_at, reverse=True) if o.campaign_id),
                    None,
                )
            ),
            "active_order_updated_at": (
                _meaningful_ts(latest_order, u.created_at) if latest_order else None
            ),
            "is_stuck": is_stuck,
            "total_paid_cents": total_paid,
            "total_credits": credit.total_credits if credit else 0,
            "used_credits": credit.used_credits if credit else 0,
            "total_emails_sent": emails_sent,
            "total_emails_replied": emails_replied,
            "total_emails_bounced": emails_bounced,
            "total_leads": total_leads,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })

    return {"users": result, "total": total}


_EXCLUDED_EMAILS = {
    "businessconnect.pranav@gmail.com",
    "pranav.hegde126@gmail.com",
    "benjenstark.578239@gmail.com",
}


@router.get("/payments")
async def admin_payments(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query("", max_length=200),
    status_filter: str = Query("paid", max_length=50),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all payment orders with user info, coupon codes, and outreach status."""

    q = (
        db.query(PaymentOrder, User, Coupon)
        .join(User, PaymentOrder.user_id == User.id)
        .outerjoin(Coupon, PaymentOrder.coupon_id == Coupon.id)
        .filter(User.email.notin_(_EXCLUDED_EMAILS))
    )

    if status_filter == "paid":
        q = q.filter(PaymentOrder.status == "paid")
    elif status_filter == "abandoned":
        q = q.filter(PaymentOrder.status == "created")
    # "all" → no status filter

    if search:
        pattern = f"%{search}%"
        q = q.filter(
            (User.name.ilike(pattern)) | (User.email.ilike(pattern))
        )

    total = q.count()
    rows = q.order_by(PaymentOrder.created_at.desc()).offset(offset).limit(limit).all()

    # Stats (always over excluded-email-filtered, paid-only, all users)
    stats_q = (
        db.query(PaymentOrder, User)
        .join(User, PaymentOrder.user_id == User.id)
        .filter(User.email.notin_(_EXCLUDED_EMAILS))
    )
    paid_rows = stats_q.filter(PaymentOrder.status == "paid").all()
    abandoned_count = stats_q.filter(PaymentOrder.status == "created").count()

    total_inr = sum(r.PaymentOrder.amount_cents for r in paid_rows if r.PaymentOrder.currency == "INR")
    total_usd = sum(r.PaymentOrder.amount_cents for r in paid_rows if r.PaymentOrder.currency == "USD")

    result = []
    for po, u, coupon in rows:
        # Latest outreach order status for this user
        latest_order = (
            db.query(OutreachOrder)
            .filter(OutreachOrder.user_id == u.id)
            .order_by(OutreachOrder.updated_at.desc())
            .first()
        )
        result.append({
            "id": po.id,
            "user_id": u.id,
            "user_name": u.name,
            "user_email": u.email,
            "amount_cents": po.amount_cents,
            "currency": po.currency,
            "provider": po.provider,
            "tier": po.tier,
            "plan_id": po.plan_id,
            "status": po.status,
            "geo_country": po.geo_country,
            "coupon_code": coupon.code if coupon else None,
            "credits_granted": po.credits_granted,
            "razorpay_order_id": po.razorpay_order_id,
            "razorpay_payment_id": po.razorpay_payment_id,
            "dodo_checkout_id": po.dodo_checkout_id,
            "dodo_payment_id": po.dodo_payment_id,
            "outreach_order_status": latest_order.status if latest_order else None,
            "created_at": po.created_at.isoformat() if po.created_at else None,
        })

    return {
        "payments": result,
        "total": total,
        "stats": {
            "total_paid": len(paid_rows),
            "total_inr_paise": total_inr,
            "total_usd_cents": total_usd,
            "total_abandoned": abandoned_count,
        },
    }


@router.get("/users/{user_id}/detail")
async def outreach_user_detail(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Full detail for a specific user's outreach activity."""
    now = datetime.now(timezone.utc)
    stuck_cutoff = now - timedelta(hours=STUCK_THRESHOLD_HOURS)

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"error": "User not found"}, 404

    # Credits
    credit = db.query(UserCredit).filter(UserCredit.user_id == user_id).first()

    # Payments
    payments = (
        db.query(PaymentOrder)
        .filter(PaymentOrder.user_id == user_id)
        .order_by(PaymentOrder.created_at.desc())
        .all()
    )

    # Orders with campaign details
    orders = (
        db.query(OutreachOrder)
        .filter(OutreachOrder.user_id == user_id)
        .order_by(OutreachOrder.created_at.desc())
        .all()
    )

    orders_data = []
    for order in orders:
        campaign_data = None
        if order.campaign_id:
            campaign = db.query(Campaign).filter(Campaign.id == order.campaign_id).first()
            if campaign:
                # Email stats for this campaign
                e_stats = (
                    db.query(EmailSent.status, func.count())
                    .filter(EmailSent.campaign_id == campaign.id)
                    .group_by(EmailSent.status)
                    .all()
                )
                email_stats = {s: c for s, c in e_stats}

                # Style breakdown
                style_stats = (
                    db.query(EmailSent.assigned_style, func.count())
                    .filter(
                        EmailSent.campaign_id == campaign.id,
                        EmailSent.assigned_style.isnot(None),
                    )
                    .group_by(EmailSent.assigned_style)
                    .all()
                )
                style_breakdown = {s: c for s, c in style_stats}

                campaign_data = {
                    "id": campaign.id,
                    "name": campaign.name,
                    "status": campaign.status,
                    "daily_limit": campaign.daily_limit,
                    "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
                    "email_stats": {
                        "queued": email_stats.get("queued", 0),
                        "scheduled": email_stats.get("scheduled", 0),
                        "sent": email_stats.get("sent", 0) + email_stats.get("replied", 0),
                        "replied": email_stats.get("replied", 0),
                        "bounced": email_stats.get("bounced", 0),
                        "failed": email_stats.get("failed", 0),
                    },
                    "style_breakdown": style_breakdown,
                }

        is_stuck = (
            order.status in SYSTEM_PROCESSING_STATUSES
            and order.updated_at
            and order.updated_at.replace(tzinfo=timezone.utc) < stuck_cutoff
        )

        # Per-stage timestamps for this specific order (per-user detail view
        # shows each order's individual journey, so we don't collapse across
        # multiple orders here).
        stage_ts = {
            key: (getattr(order, column).isoformat() if getattr(order, column, None) else None)
            for key, _label, column in FUNNEL_STAGES
        }

        orders_data.append({
            "id": order.id,
            # Surfaced so the admin panel can open the candidate profile editor.
            "candidate_id": order.candidate_id,
            "status": order.status,
            "leads_collected": order.leads_collected,
            "leads_target": order.leads_target,
            "is_stuck": is_stuck,
            "action_log": order.action_log or [],
            "stage_timestamps": stage_ts,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            "campaign": campaign_data,
        })

    # Lead summary
    candidate_ids = [o.candidate_id for o in orders if o.candidate_id]
    lead_summary = {"total": 0, "with_email": 0, "email_verified": 0, "avg_score": 0}
    if candidate_ids:
        lead_summary["total"] = (
            db.query(func.count()).select_from(Lead)
            .filter(Lead.candidate_id.in_(candidate_ids)).scalar()
        ) or 0
        lead_summary["with_email"] = (
            db.query(func.count()).select_from(Lead)
            .filter(Lead.candidate_id.in_(candidate_ids), Lead.email.isnot(None)).scalar()
        ) or 0
        lead_summary["email_verified"] = (
            db.query(func.count()).select_from(Lead)
            .filter(Lead.candidate_id.in_(candidate_ids), Lead.email_verified == True).scalar()
        ) or 0
        from database.models import LeadScore
        avg = (
            db.query(func.avg(LeadScore.overall_score))
            .join(Lead, Lead.id == LeadScore.lead_id)
            .filter(Lead.candidate_id.in_(candidate_ids))
            .scalar()
        )
        lead_summary["avg_score"] = round(float(avg), 1) if avg else 0

    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "credits": {
            "total": credit.total_credits if credit else 0,
            "used": credit.used_credits if credit else 0,
            "available": (credit.total_credits - credit.used_credits) if credit else 0,
        },
        "payments": [
            {
                "id": p.id,
                "amount_cents": p.amount_cents,
                "currency": p.currency,
                "tier": p.tier,
                "plan_id": p.plan_id,
                "status": p.status,
                "credits_granted": p.credits_granted,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in payments
        ],
        "orders": orders_data,
        "lead_summary": lead_summary,
    }


@router.get("/signups")
async def signups_by_date(
    start: str = Query(..., description="Start date YYYY-MM-DD (inclusive)"),
    end: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Return signup counts from the User table for a date range.
    Used by the analytics dashboard for accurate new-signup metrics.
    """
    try:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start date, expected YYYY-MM-DD")
    try:
        end_dt = (datetime.fromisoformat(end) + timedelta(days=1)).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid end date, expected YYYY-MM-DD")

    total = (
        db.query(func.count(User.id))
        .filter(User.created_at >= start_dt, User.created_at < end_dt)
        .scalar()
    ) or 0

    daily_rows = (
        db.query(
            func.date_trunc("day", User.created_at).label("day"),
            func.count(User.id).label("signups"),
        )
        .filter(User.created_at >= start_dt, User.created_at < end_dt)
        .group_by("day")
        .order_by("day")
        .all()
    )

    return {
        "count": total,
        "daily": [
            {"day": row.day.strftime("%Y-%m-%d"), "signups": row.signups}
            for row in daily_rows
        ],
    }


@router.get("/campaign/{campaign_id}/emails")
async def admin_campaign_emails(
    campaign_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin-only: return all emails for any campaign, bypassing user ownership check."""
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    emails = (
        db.query(EmailSent)
        .filter(EmailSent.campaign_id == campaign_id)
        .order_by(func.coalesce(EmailSent.scheduled_at, EmailSent.created_at).asc())
        .all()
    )

    result = []
    for email in emails:
        lead = db.query(Lead).filter_by(id=email.lead_id).first() if email.lead_id else None
        result.append({
            "id": email.id,
            "lead_name": lead.name if lead else "Unknown",
            "lead_company": lead.company if lead else "",
            "lead_title": lead.title if lead else "",
            "to_email": email.to_email,
            "subject": email.subject,
            "body": email.body,
            "status": email.status,
            "assigned_style": email.assigned_style,
            "sent_at": email.sent_at.isoformat() if email.sent_at else None,
            "reply_text": email.reply_text,
            "reply_sentiment": email.reply_sentiment,
            "reply_received_at": email.reply_received_at.isoformat() if email.reply_received_at else None,
            "bounce_reason": email.bounce_reason,
            "error_message": email.error_message,
            "first_opened_at": email.first_opened_at.isoformat() if email.first_opened_at else None,
            "open_count": email.open_count or 0,
        })

    # Resolve the campaign owner via the linked order
    order = db.query(OutreachOrder).filter(OutreachOrder.campaign_id == campaign_id).first()
    owner = db.query(User).filter(User.id == order.user_id).first() if order else None

    return {
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
            "daily_limit": campaign.daily_limit,
        },
        "owner": {
            "id": owner.id if owner else None,
            "name": owner.name if owner else "Unknown",
            "email": owner.email if owner else "",
        },
        "emails": result,
    }


# ---------------------------------------------------------------------------
# Paid-user funnel — powers the Campaign Health admin dashboard.
#
# Design decisions (from verified ground-truth analysis):
#
# 1. GMAIL CONNECTED truth: Use email_accounts table existence, NOT
#    gmail_connected_at. The timestamp column was not written for users who
#    onboarded before it was tracked. email_accounts is always created when
#    OAuth completes and is never deleted by normal flow, making it the
#    authoritative source. gmail_connected_at is used only as a fallback
#    timestamp for WHEN they connected.
#
# 2. EMAIL COUNTS: emails_sent stores follow-up emails as separate rows
#    (followup_number > 0). "Leads contacted" = initial emails only.
#    Follow-ups reported separately so the numbers are meaningful.
#
# 3. DEDUPLICATION: Users are deduplicated at the Python level. Multiple
#    outreach orders per user are collapsed — funnel timestamps take the
#    earliest across all orders; best campaign is the most-active one.
#
# 4. CAMPAIGN SELECTION per user: running > paused > completed > cancelled.
#    Email stats are shown for that best campaign only (not summed across
#    all campaigns, which would conflate old test runs with real campaigns).
# ---------------------------------------------------------------------------

_EXCLUDED_FUNNEL_EMAILS = {
    "businessconnect.pranav@gmail.com",
    "pranav.hegde126@gmail.com",
    "pranavshastry4@gmail.com",
    "studojo@gmail.com",
    "benjenstark.578239@gmail.com",
    "pranavs.hegde@bbafeh.christuniversity.in",
}

_FUNNEL_STAGE_COLS = [
    ("resume_uploaded",      "resume_uploaded_at"),
    ("quiz_completed",       "quiz_completed_at"),
    ("leads_generated",      "leads_generated_at"),
    ("gmail_connected",      "gmail_connected_at"),   # timestamp only; presence checked via email_accounts
    ("email_style_selected", "email_style_selected_at"),
    ("campaign_launched",    "campaign_launched_at"),
    ("campaign_paused",      "campaign_paused_at"),
    ("campaign_completed",   "campaign_completed_at"),
]

_CAMPAIGN_PRIORITY = {"running": 0, "paused": 1, "completed": 2, "cancelled": 3, "draft": 4}


@router.get("/paid-funnel")
async def paid_funnel(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    All real paid users with full funnel state, campaign health, and profile.
    Powers the Campaign Health admin dashboard.
    """
    paid_payments = (
        db.query(PaymentOrder, User)
        .join(User, PaymentOrder.user_id == User.id)
        .filter(
            PaymentOrder.status == "paid",
            PaymentOrder.amount_cents > 0,
            PaymentOrder.provider != "coupon",
            User.email.notin_(_EXCLUDED_FUNNEL_EMAILS),
        )
        .order_by(PaymentOrder.created_at.asc())
        .all()
    )

    # One bucket per user — collect all payment rows
    buckets: dict[str, dict] = {}
    for po, u in paid_payments:
        if u.id not in buckets:
            buckets[u.id] = {"user": u, "payments": []}
        buckets[u.id]["payments"].append(po)

    result = []
    for user_id, bucket in buckets.items():
        u: User = bucket["user"]
        payments: list[PaymentOrder] = bucket["payments"]
        first_paid_at = min(p.created_at for p in payments)
        total_paid_cents = sum(p.amount_cents for p in payments)
        currency = payments[0].currency

        # ── All outreach orders ────────────────────────────────────────────
        orders: list[OutreachOrder] = (
            db.query(OutreachOrder)
            .filter(OutreachOrder.user_id == user_id)
            .order_by(OutreachOrder.created_at.asc())
            .all()
        )

        # ── Funnel timestamps (earliest across all orders) ─────────────────
        # campaign_paused_at and campaign_completed_at on outreach_orders are
        # unreliable — they can be set on old/dummy orders whose campaign is
        # now in a different state. We derive these from campaign rows instead
        # (done below) so we exclude them from the order-level collapse here.
        _SKIP_FROM_ORDERS = {"campaign_paused", "campaign_completed"}
        stage_ts: dict[str, str | None] = {}
        for key, col in _FUNNEL_STAGE_COLS:
            if key in _SKIP_FROM_ORDERS:
                stage_ts[key] = None  # will be set from campaign rows below
                continue
            best = None
            for o in orders:
                v = getattr(o, col, None)
                if v and (best is None or v < best):
                    best = v
            stage_ts[key] = best.isoformat() if best else None

        # ── Gmail — authoritative source: email_accounts table ────────────
        email_accounts: list[EmailAccount] = (
            db.query(EmailAccount)
            .filter(EmailAccount.user_id == user_id)
            .order_by(EmailAccount.created_at.asc())
            .all()
        )
        gmail_connected = len(email_accounts) > 0
        if gmail_connected and not stage_ts.get("gmail_connected"):
            stage_ts["gmail_connected"] = email_accounts[0].created_at.isoformat()

        # ── Collect unique campaigns ───────────────────────────────────────
        seen_campaign_ids: set[int] = set()
        all_campaigns: list[Campaign] = []
        for o in orders:
            if o.campaign_id and o.campaign_id not in seen_campaign_ids:
                c = db.query(Campaign).filter(Campaign.id == o.campaign_id).first()
                if c:
                    all_campaigns.append(c)
                    seen_campaign_ids.add(c.id)

        # ── Back-fill ALL missing timestamps from campaign + account facts ─
        #
        # Many outreach_orders timestamp columns were never written for early
        # users. The campaign table and email_accounts are always written by
        # the system (not the user flow), so they are the authoritative source.
        #
        # Rule: if a campaign exists for the user, every stage UP TO AND
        # INCLUDING campaign_launched definitively happened. Back-fill any
        # missing timestamps using the earliest campaign's dates as a proxy.
        #
        # For resume/quiz/leads: if the user has a campaign, they passed those
        # gates — use the earliest outreach_order.created_at as the timestamp
        # since that's when they started the flow.
        if all_campaigns or gmail_connected:
            # The earliest order created_at is the best proxy for when the
            # user first started the outreach flow
            earliest_order_ts = (
                min((o.created_at for o in orders if o.created_at), default=None)
            )
            earliest_campaign = (
                min(all_campaigns, key=lambda c: c.created_at or datetime.min, default=None)
            )

            # resume, quiz, leads: if missing and user got to campaign, infer from order start
            if earliest_order_ts:
                for key in ("resume_uploaded", "quiz_completed", "leads_generated"):
                    if not stage_ts.get(key):
                        stage_ts[key] = earliest_order_ts.isoformat()

            # gmail: already handled above via email_accounts

            # style, launch: derive from campaign.created_at / started_at
            if earliest_campaign:
                if not stage_ts.get("email_style_selected") and earliest_campaign.created_at:
                    stage_ts["email_style_selected"] = earliest_campaign.created_at.isoformat()
                if not stage_ts.get("campaign_launched"):
                    ts = earliest_campaign.started_at or earliest_campaign.created_at
                    if ts:
                        stage_ts["campaign_launched"] = ts.isoformat()

            # paused / completed: only trust campaign.paused_at/completed_at
            # when the campaign is actually in that state. A campaign that was
            # paused then resumed will have paused_at set but status='running' —
            # we still show Pause dot for that. A campaign with completed_at
            # must have status='completed' to count as done.
            for c in all_campaigns:
                if getattr(c, "paused_at", None):
                    if not stage_ts.get("campaign_paused"):
                        stage_ts["campaign_paused"] = c.paused_at.isoformat()
                if getattr(c, "completed_at", None) and c.status == "completed":
                    if not stage_ts.get("campaign_completed"):
                        stage_ts["campaign_completed"] = c.completed_at.isoformat()

        best_campaign: Campaign | None = (
            min(all_campaigns, key=lambda c: _CAMPAIGN_PRIORITY.get(c.status, 5), default=None)
        )

        # ── Campaign stats (initial emails only — no follow-up inflation) ──
        campaign_data = None
        if best_campaign:
            is_initial = (
                (EmailSent.followup_number == None) | (EmailSent.followup_number == 0)
            )
            is_followup = (
                EmailSent.followup_number.isnot(None) & (EmailSent.followup_number > 0)
            )

            def _stats(cid: int, filt=None) -> dict:
                q = db.query(EmailSent.status, func.count()).filter(
                    EmailSent.campaign_id == cid
                )
                if filt is not None:
                    q = q.filter(filt)
                return {s: n for s, n in q.group_by(EmailSent.status).all()}

            init = _stats(best_campaign.id, is_initial)
            fups = _stats(best_campaign.id, is_followup)

            leads_contacted = init.get("sent", 0) + init.get("replied", 0)
            replied         = init.get("replied", 0)
            bounced         = init.get("bounced", 0)
            failed          = init.get("failed", 0)
            queued          = (
                db.query(func.count()).select_from(EmailSent)
                .filter(
                    EmailSent.campaign_id == best_campaign.id,
                    EmailSent.status.in_(["queued", "pending_enrichment"]),
                    is_initial,
                ).scalar() or 0
            )
            last_sent = (
                db.query(func.max(EmailSent.sent_at))
                .filter(EmailSent.campaign_id == best_campaign.id)
                .scalar()
            )
            # ── Failure breakdown (for breaking campaign detail panel) ──────
            # Auth failures scoped to last 7 days so reconnected users drop off the alert.
            # Enrichment scoped to initial emails only to stay consistent with the displayed `failed` stat.
            # Bad-email category removed — was misclassifying Gmail API "Requested entity was not found"
            # as a recipient bounce.
            from sqlalchemy import or_ as sa_or, and_ as sa_and
            _recent_window = datetime.utcnow() - timedelta(days=7)
            def _fail_count(condition) -> int:
                return (
                    db.query(func.count()).select_from(EmailSent)
                    .filter(EmailSent.campaign_id == best_campaign.id, condition)
                    .scalar() or 0
                )

            enrichment_failures = _fail_count(
                sa_and(EmailSent.status == "failed",
                       is_initial,
                       EmailSent.error_message.ilike("%enrichment failed%"))
            )
            auth_failures = _fail_count(
                sa_and(EmailSent.status == "failed",
                       EmailSent.created_at > _recent_window,
                       sa_or(
                           EmailSent.error_message.ilike("%invalid authentication%"),
                           EmailSent.error_message.ilike("%authError%"),
                           EmailSent.error_message.ilike('%"code": 401%'),
                       ))
            )
            # Sample error messages (top 3 distinct)
            sample_errors = [
                r[0] for r in
                db.query(EmailSent.error_message)
                .filter(
                    EmailSent.campaign_id == best_campaign.id,
                    EmailSent.status == "failed",
                    EmailSent.error_message.isnot(None),
                )
                .group_by(EmailSent.error_message)
                .order_by(func.count().desc())
                .limit(3)
                .all()
                if r[0]
            ]
            # Get email account token expiry
            from database.models import EmailAccount as EA
            email_account = (
                db.query(EA).filter(EA.id == best_campaign.email_account_id).first()
                if best_campaign.email_account_id else None
            )
            token_expiry = email_account.token_expiry.isoformat() if email_account and email_account.token_expiry else None

            # Replacement counts for auto-replenishment dashboard
            cid = best_campaign.id
            replacements_bounce = (
                db.query(func.count(EmailSent.id))
                .filter(EmailSent.campaign_id == cid,
                        EmailSent.replacement_reason == "bounce")
                .scalar()
            ) or 0
            replacements_enrichment = (
                db.query(func.count(EmailSent.id))
                .filter(EmailSent.campaign_id == cid,
                        EmailSent.replacement_reason == "enrichment_exhausted")
                .scalar()
            ) or 0
            initial_lead_count = (
                db.query(func.count(EmailSent.id))
                .filter(
                    EmailSent.campaign_id == cid,
                    EmailSent.replacement_for_id.is_(None),
                    sa_or(EmailSent.followup_number == 0, EmailSent.followup_number.is_(None)),
                )
                .scalar()
            ) or 0
            replacement_cap = math.ceil(initial_lead_count * 0.25)
            replacements_total = replacements_bounce + replacements_enrichment

            campaign_data = {
                "id": best_campaign.id,
                "name": best_campaign.name,
                "status": best_campaign.status,
                "daily_limit": best_campaign.daily_limit,
                "started_at": best_campaign.started_at.isoformat() if best_campaign.started_at else None,
                "gmail_account": email_account.email_address if email_account else None,
                "token_expiry": token_expiry,
                "stats": {
                    "leads_contacted": leads_contacted,
                    "replied": replied,
                    "bounced": bounced,
                    "failed": failed,
                    "queued": queued,
                    "reply_rate": round((replied + fups.get("replied", 0)) / leads_contacted * 100, 1) if leads_contacted > 0 else 0,
                    "followups_sent": fups.get("sent", 0) + fups.get("replied", 0),
                    "followups_replied": fups.get("replied", 0),
                    "last_email_sent": last_sent.isoformat() if last_sent else None,
                    "failure_breakdown": {
                        "enrichment": enrichment_failures,
                        "auth": auth_failures,
                        "other": max(0, failed - enrichment_failures - auth_failures),
                    },
                    "sample_errors": sample_errors,
                    "replacements_bounce": replacements_bounce,
                    "replacements_enrichment": replacements_enrichment,
                    "replacement_cap_remaining": max(0, replacement_cap - replacements_total),
                },
            }

        # ── Candidate profile + lead quality ──────────────────────────────
        candidate: Candidate | None = next(
            (db.query(Candidate).filter(Candidate.id == o.candidate_id).first()
             for o in orders if o.candidate_id),
            None,
        )
        profile_data = None
        lead_quality = None
        if candidate:
            profile_data = {
                "target_roles": candidate.target_roles or [],
                "target_industries": candidate.target_industries or [],
                "dream_companies": candidate.dream_companies or [],
                "resume_profile": candidate.resume_profile or {},
                "psychometric_profile": candidate.psychometric_profile or {},
                "flex_notes": candidate.flex_notes or {},
            }
            total_leads = (
                db.query(func.count()).select_from(Lead)
                .filter(Lead.candidate_id == candidate.id).scalar()
            ) or 0
            leads_with_email = (
                db.query(func.count()).select_from(Lead)
                .filter(Lead.candidate_id == candidate.id, Lead.email.isnot(None)).scalar()
            ) or 0
            leads_verified = (
                db.query(func.count()).select_from(Lead)
                .filter(Lead.candidate_id == candidate.id, Lead.email_verified == True).scalar()
            ) or 0
            score_agg = (
                db.query(
                    func.avg(LeadScore.overall_score).label("avg"),
                    func.avg(LeadScore.title_relevance).label("avg_title"),
                    func.avg(LeadScore.department_relevance).label("avg_dept"),
                    func.avg(LeadScore.industry_relevance).label("avg_industry"),
                    func.avg(LeadScore.seniority_relevance).label("avg_seniority"),
                    func.avg(LeadScore.location_relevance).label("avg_location"),
                    func.sum(case((LeadScore.overall_score >= 80, 1), else_=0)).label("high"),
                    func.sum(case(
                        ((LeadScore.overall_score >= 60) & (LeadScore.overall_score < 80), 1), else_=0
                    )).label("medium"),
                    func.sum(case((LeadScore.overall_score < 60, 1), else_=0)).label("low"),
                )
                .join(Lead, Lead.id == LeadScore.lead_id)
                .filter(Lead.candidate_id == candidate.id)
                .first()
            )
            avg_score = score_agg.avg if score_agg else None
            industry_rows = (
                db.query(Lead.industry, func.count())
                .filter(Lead.candidate_id == candidate.id, Lead.industry.isnot(None))
                .group_by(Lead.industry).order_by(func.count().desc()).limit(8).all()
            )
            title_rows = (
                db.query(Lead.title, func.count())
                .filter(Lead.candidate_id == candidate.id, Lead.title.isnot(None))
                .group_by(Lead.title).order_by(func.count().desc()).limit(8).all()
            )
            def _r(v) -> float | None:
                return round(float(v), 1) if v is not None else None

            lead_quality = {
                "total": total_leads,
                "with_email": leads_with_email,
                "email_verified": leads_verified,
                "avg_score": _r(avg_score),
                "score_breakdown": {
                    "title":      _r(score_agg.avg_title)      if score_agg else None,
                    "department": _r(score_agg.avg_dept)       if score_agg else None,
                    "industry":   _r(score_agg.avg_industry)   if score_agg else None,
                    "seniority":  _r(score_agg.avg_seniority)  if score_agg else None,
                    "location":   _r(score_agg.avg_location)   if score_agg else None,
                },
                "score_distribution": {
                    "high":   int(score_agg.high   or 0) if score_agg else 0,
                    "medium": int(score_agg.medium or 0) if score_agg else 0,
                    "low":    int(score_agg.low    or 0) if score_agg else 0,
                },
                "email_rate": round(leads_with_email / total_leads * 100, 1) if total_leads > 0 else 0,
                "verified_rate": round(leads_verified / leads_with_email * 100, 1) if leads_with_email > 0 else 0,
                "top_industries": [{"label": r[0], "count": r[1]} for r in industry_rows],
                "top_titles": [{"label": r[0], "count": r[1]} for r in title_rows],
            }

        # ── Funnel status (single source of truth) ─────────────────────────
        # Derived from actual data, not from order.status which can be stale.
        any_completed = any(c.status == "completed" for c in all_campaigns)
        any_running   = any(c.status == "running"   for c in all_campaigns)
        any_paused    = any(c.status == "paused"    for c in all_campaigns)
        any_cancelled = any(c.status == "cancelled" for c in all_campaigns)
        any_launched  = bool(stage_ts.get("campaign_launched"))

        if any_completed and not any_running and not any_paused:
            funnel_status = "completed"
        elif any_running:
            funnel_status = "running"
        elif any_paused:
            funnel_status = "paused"
        elif any_cancelled:
            funnel_status = "cancelled"
        elif any_launched:
            funnel_status = "launched"
        elif gmail_connected:
            funnel_status = "gmail_connected"
        elif stage_ts.get("leads_generated"):
            funnel_status = "leads_generated"
        elif stage_ts.get("resume_uploaded"):
            funnel_status = "resume_uploaded"
        else:
            funnel_status = "paid_only"

        result.append({
            "user_id": user_id,
            "name": u.name,
            "email": u.email,
            "paid_at": first_paid_at.isoformat(),
            "total_paid_cents": total_paid_cents,
            "currency": currency,
            "gmail_connected": gmail_connected,
            "funnel_status": funnel_status,
            "stage_timestamps": stage_ts,
            "campaign": campaign_data,
            "profile": profile_data,
            "lead_quality": lead_quality,
        })

    # Sort: most recently paid first
    result.sort(key=lambda r: r["paid_at"], reverse=True)
    return {"users": result, "total": len(result)}

# ── Candidate profile editor ──────────────────────────────────────────────────
#
# Support tooling for correcting a mis-parsed candidate profile. Before this
# existed there was NO way for staff to fix a bad resume parse: every write path
# that touches `candidates` is scoped to `current_user.id`, so a paying customer
# with wrong skills had to redo onboarding themselves.
#
# Everything here writes to the fields the lead-search and email-generation
# pipelines actually read. The precedence rules below are load-bearing — editing
# the "obvious" field alone silently does nothing in several cases:
#
#   target role   flex_notes.target_role_user_input  OVERRIDES  target_roles
#                 (_derive_role_and_locations, routes_discovery.py)
#   roles used    parsed_json.career_analysis.recommended_roles[].title
#                 falls back to candidates.target_roles only when empty
#                 (routes_discovery.py, the /search payload builder)
#   skills        parsed_json.personal_info.skills_detected
#                 falls back to parsed_json.skills
#                 (extract_candidate_profile, email_generator_service.py)
#
# So a role edit writes all three places, and a skills edit writes both.

_PROFILE_EDITABLE_FIELDS = {
    "target_role", "skills", "locations", "company_size", "company_stage",
    "industries", "niche_keywords", "work_mode", "dream_companies",
    "extra_manager_titles",
    "best_project", "outcome", "why_now", "credential",
}


class CandidateProfilePatch(BaseModel):
    """All operator-editable profile fields. Every field is optional —
    omitted fields are left untouched, so a caller can PATCH one thing."""
    target_role: Optional[str] = None
    skills: Optional[List[str]] = None          # ordered; first 3 become email headline
    locations: Optional[List[str]] = None
    company_size: Optional[str] = None          # Apollo range, e.g. "50,500" or "1,10000"
    company_stage: Optional[str] = None
    industries: Optional[List[str]] = None
    niche_keywords: Optional[List[str]] = None
    work_mode: Optional[str] = None
    dream_companies: Optional[List[str]] = None
    # Extra hiring-manager titles merged into every Apollo segment. Use to widen
    # targeting into a function the resume alone would not imply.
    extra_manager_titles: Optional[List[str]] = None
    # flex_notes — these feed the email's SENDER SIGNAL directly
    best_project: Optional[str] = None
    outcome: Optional[str] = None
    why_now: Optional[str] = None
    credential: Optional[str] = None
    # audit
    reason: Optional[str] = None                # why the operator made this edit


def _clean_str_list(v) -> list:
    """Trim, drop blanks, de-duplicate preserving order."""
    if not isinstance(v, list):
        return []
    seen, out = set(), []
    for item in v:
        s = str(item).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


@router.get("/candidates/{candidate_id}/profile")
async def admin_get_candidate_profile(
    candidate_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Current editable profile for a candidate, in the same shape PATCH accepts.

    Returns the effective values — i.e. what the pipeline will actually use,
    resolving the precedence rules rather than showing raw columns.
    """
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    parsed = candidate.parsed_json if isinstance(candidate.parsed_json, dict) else {}
    personal = parsed.get("personal_info") if isinstance(parsed.get("personal_info"), dict) else {}
    prefs = parsed.get("preferences") if isinstance(parsed.get("preferences"), dict) else {}
    career = parsed.get("career_analysis") if isinstance(parsed.get("career_analysis"), dict) else {}
    flex = candidate.flex_notes if isinstance(candidate.flex_notes, dict) else {}

    # Effective target role, mirroring _derive_role_and_locations precedence.
    role = (flex.get("target_role_user_input") or "").strip()
    role_source = "flex_notes.target_role_user_input"
    if not role:
        rec = career.get("recommended_roles") or []
        if rec and isinstance(rec[0], dict):
            role = (rec[0].get("title") or "").strip()
            role_source = "parsed_json.career_analysis.recommended_roles[0].title"
    if not role:
        tr = candidate.target_roles if isinstance(candidate.target_roles, list) else []
        if tr:
            first = tr[0]
            role = first if isinstance(first, str) else (first.get("title") or first.get("role") or "")
            role_source = "candidates.target_roles[0]"

    skills = personal.get("skills_detected") or parsed.get("skills") or []

    user = db.query(User).filter(User.id == candidate.user_id).first()

    return {
        "candidate_id": candidate.id,
        "user_id": candidate.user_id,
        "user_email": user.email if user else None,
        "user_name": user.name if user else None,
        "profile": {
            "target_role": role,
            "skills": _clean_str_list(skills),
            "locations": _clean_str_list(prefs.get("locations")),
            "company_size": prefs.get("company_size") or "",
            "company_stage": prefs.get("company_stage") or "",
            "industries": _clean_str_list(prefs.get("industry_interests")),
            "niche_keywords": _clean_str_list(prefs.get("niche_keywords")),
            "work_mode": prefs.get("work_mode") or "",
            "extra_manager_titles": _clean_str_list(prefs.get("extra_manager_titles")),
            "dream_companies": _clean_str_list(candidate.dream_companies),
            "best_project": flex.get("best_project") or "",
            "outcome": flex.get("outcome") or "",
            "why_now": flex.get("why_now") or "",
            "credential": flex.get("credential") or "",
        },
        "target_role_source": role_source,
        "recommended_roles": [
            r.get("title") for r in (career.get("recommended_roles") or [])
            if isinstance(r, dict) and r.get("title")
        ],
        "edit_history": [
            e for e in (candidate.parsed_json or {}).get("_admin_edits", [])
        ] if isinstance(candidate.parsed_json, dict) else [],
    }


@router.patch("/candidates/{candidate_id}/profile")
async def admin_patch_candidate_profile(
    candidate_id: int,
    patch: CandidateProfilePatch,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Correct a candidate's profile. Omitted fields are left untouched.

    Every edit appends a before/after entry to parsed_json._admin_edits so the
    change is attributable and reversible.
    """
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Deep-copy the JSONB blobs. SQLAlchemy does not track in-place mutation of
    # a JSONB dict, so mutating candidate.parsed_json directly would not persist.
    parsed = copy.deepcopy(candidate.parsed_json) if isinstance(candidate.parsed_json, dict) else {}
    flex = copy.deepcopy(candidate.flex_notes) if isinstance(candidate.flex_notes, dict) else {}
    parsed.setdefault("personal_info", {})
    parsed.setdefault("preferences", {})
    parsed.setdefault("career_analysis", {})

    personal = parsed["personal_info"]
    prefs = parsed["preferences"]
    career = parsed["career_analysis"]

    changes = {}

    def _record(field, before, after):
        if before != after:
            changes[field] = {"before": before, "after": after}

    # ── target role: write all three places precedence can read ──────────────
    if patch.target_role is not None:
        role = patch.target_role.strip()
        before = flex.get("target_role_user_input") or ""
        # 1. highest-precedence field, read by _derive_role_and_locations
        flex["target_role_user_input"] = role
        # 2. recommended_roles[0].title, read by the /search payload builder
        rec = career.get("recommended_roles")
        if not isinstance(rec, list) or not rec:
            rec = [{}]
        if not isinstance(rec[0], dict):
            rec[0] = {}
        rec[0]["title"] = role
        rec[0].setdefault("seniority", "entry")
        career["recommended_roles"] = rec
        # 3. the column, used as the final fallback
        candidate.target_roles = [role] if role else []
        _record("target_role", before, role)

    # ── skills: order matters, first 3 become the email headline ─────────────
    if patch.skills is not None:
        cleaned = _clean_str_list(patch.skills)
        before = _clean_str_list(personal.get("skills_detected") or parsed.get("skills"))
        personal["skills_detected"] = cleaned
        if "skills" in parsed:            # keep the fallback key consistent
            parsed["skills"] = cleaned
        _record("skills", before, cleaned)

    # ── preferences ──────────────────────────────────────────────────────────
    if patch.locations is not None:
        cleaned = _clean_str_list(patch.locations)
        _record("locations", _clean_str_list(prefs.get("locations")), cleaned)
        prefs["locations"] = cleaned

    if patch.company_size is not None:
        _record("company_size", prefs.get("company_size") or "", patch.company_size.strip())
        prefs["company_size"] = patch.company_size.strip()

    if patch.company_stage is not None:
        _record("company_stage", prefs.get("company_stage") or "", patch.company_stage.strip())
        prefs["company_stage"] = patch.company_stage.strip()

    if patch.industries is not None:
        cleaned = _clean_str_list(patch.industries)
        _record("industries", _clean_str_list(prefs.get("industry_interests")), cleaned)
        prefs["industry_interests"] = cleaned

    if patch.niche_keywords is not None:
        cleaned = _clean_str_list(patch.niche_keywords)
        _record("niche_keywords", _clean_str_list(prefs.get("niche_keywords")), cleaned)
        prefs["niche_keywords"] = cleaned

    if patch.work_mode is not None:
        _record("work_mode", prefs.get("work_mode") or "", patch.work_mode.strip())
        prefs["work_mode"] = patch.work_mode.strip()

    if patch.extra_manager_titles is not None:
        # Cap at 30: _merge_extra_titles holds each segment under 60 titles, above
        # which Apollo result quality degrades.
        cleaned = _clean_str_list(patch.extra_manager_titles)[:30]
        _record("extra_manager_titles",
                _clean_str_list(prefs.get("extra_manager_titles")), cleaned)
        prefs["extra_manager_titles"] = cleaned

    # ── dream companies (own column) ─────────────────────────────────────────
    if patch.dream_companies is not None:
        cleaned = _clean_str_list(patch.dream_companies)[:10]  # same cap as the quiz
        _record("dream_companies", _clean_str_list(candidate.dream_companies), cleaned)
        candidate.dream_companies = cleaned

    # ── flex_notes: these feed SENDER SIGNAL in every generated email ────────
    for field in ("best_project", "outcome", "why_now", "credential"):
        val = getattr(patch, field)
        if val is not None:
            _record(field, flex.get(field) or "", val.strip())
            flex[field] = val.strip()

    if not changes:
        return {"ok": True, "candidate_id": candidate.id, "changed": {}, "note": "no changes"}

    # ── audit trail ──────────────────────────────────────────────────────────
    history = parsed.get("_admin_edits")
    if not isinstance(history, list):
        history = []
    history.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "admin_id": admin.id,
        "admin_email": admin.email,
        "reason": (patch.reason or "").strip(),
        "changes": changes,
    })
    parsed["_admin_edits"] = history[-50:]  # keep the last 50 edits

    candidate.parsed_json = parsed
    candidate.flex_notes = flex
    db.commit()

    return {
        "ok": True,
        "candidate_id": candidate.id,
        "changed": changes,
        "note": (
            "Profile updated. Leads already collected are unaffected — "
            "re-run lead discovery to apply the new targeting. Emails not yet "
            "sent will use the new skills, since bodies are generated at send time."
        ),
    }


class LeadResetRequest(BaseModel):
    """Options for clearing a candidate's lead set before a re-run."""
    # Leads that already have an email sent/queued against them are preserved by
    # default: emails_sent.lead_id is ON DELETE CASCADE, so removing such a lead
    # destroys the delivery record and any reply history with it.
    keep_contacted: bool = True
    reason: Optional[str] = None


@router.post("/candidates/{candidate_id}/leads/reset")
async def admin_reset_candidate_leads(
    candidate_id: int,
    request: LeadResetRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Clear a candidate's leads so the next discovery run starts clean.

    Lead collection APPENDS — `_store_people` deduplicates against existing rows
    but never removes them. After a profile edit that changes targeting, a
    re-run therefore stacks correctly-targeted leads on top of leads found under
    the old criteria, and the stale ones keep their scores and stay visible.

    Safety: leads with an EmailSent row are retained unless keep_contacted is
    false, because emails_sent.lead_id cascades on delete and would take the
    send/reply history with it.
    """
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    total = db.query(func.count(Lead.id)).filter(Lead.candidate_id == candidate_id).scalar() or 0

    contacted_ids = [
        row[0] for row in (
            db.query(EmailSent.lead_id)
            .join(Lead, EmailSent.lead_id == Lead.id)
            .filter(Lead.candidate_id == candidate_id)
            .distinct()
            .all()
        ) if row[0] is not None
    ]

    q = db.query(Lead).filter(Lead.candidate_id == candidate_id)
    if request.keep_contacted and contacted_ids:
        q = q.filter(~Lead.id.in_(contacted_ids))

    deleted = q.delete(synchronize_session=False)
    db.commit()

    # Record on the candidate so the reset shows up beside profile edits.
    parsed = copy.deepcopy(candidate.parsed_json) if isinstance(candidate.parsed_json, dict) else {}
    history = parsed.get("_admin_edits")
    if not isinstance(history, list):
        history = []
    history.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "admin_id": admin.id,
        "admin_email": admin.email,
        "reason": (request.reason or "").strip() or "Lead reset",
        "changes": {"leads": {"before": total, "after": total - deleted}},
    })
    parsed["_admin_edits"] = history[-50:]
    candidate.parsed_json = parsed
    db.commit()

    return {
        "ok": True,
        "candidate_id": candidate_id,
        "deleted": deleted,
        "kept_contacted": len(contacted_ids) if request.keep_contacted else 0,
        "remaining": total - deleted,
        "note": (
            "Leads cleared. Run lead discovery again to collect leads matching "
            "the updated profile."
        ),
    }
