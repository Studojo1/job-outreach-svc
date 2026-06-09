"""Admin endpoints for outreach order monitoring dashboard."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
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

    # Email stats
    email_stats = (
        db.query(
            EmailSent.status,
            func.count(),
        )
        .group_by(EmailSent.status)
        .all()
    )
    email_map = {s: c for s, c in email_stats}
    total_sent = email_map.get("sent", 0) + email_map.get("replied", 0)
    total_replied = email_map.get("replied", 0)
    total_bounced = email_map.get("bounced", 0)
    reply_rate_pct = round((total_replied / total_sent * 100), 1) if total_sent > 0 else 0.0

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

    monthly_metrics = sorted(
        [
            {
                "month": m,
                "orders_created": d.get("orders_created", 0),
                "revenue_cents": d.get("revenue_cents", 0),
                "emails_sent": d.get("emails_sent", 0),
                "emails_replied": d.get("emails_replied", 0),
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
        "reply_rate_pct": reply_rate_pct,
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
        stage_ts: dict[str, str | None] = {}
        for key, col in _FUNNEL_STAGE_COLS:
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

            # paused / completed: derive from campaign.paused_at / completed_at
            for c in all_campaigns:
                if not stage_ts.get("campaign_paused") and getattr(c, "paused_at", None):
                    stage_ts["campaign_paused"] = c.paused_at.isoformat()
                if not stage_ts.get("campaign_completed") and getattr(c, "completed_at", None):
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
            campaign_data = {
                "id": best_campaign.id,
                "name": best_campaign.name,
                "status": best_campaign.status,
                "daily_limit": best_campaign.daily_limit,
                "started_at": best_campaign.started_at.isoformat() if best_campaign.started_at else None,
                "stats": {
                    "leads_contacted": leads_contacted,
                    "replied": replied,
                    "bounced": bounced,
                    "failed": failed,
                    "queued": queued,
                    "reply_rate": round(replied / leads_contacted * 100, 1) if leads_contacted > 0 else 0,
                    "followups_sent": fups.get("sent", 0) + fups.get("replied", 0),
                    "followups_replied": fups.get("replied", 0),
                    "last_email_sent": last_sent.isoformat() if last_sent else None,
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
            avg_score = (
                db.query(func.avg(LeadScore.overall_score))
                .join(Lead, Lead.id == LeadScore.lead_id)
                .filter(Lead.candidate_id == candidate.id)
                .scalar()
            )
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
            lead_quality = {
                "total": total_leads,
                "with_email": leads_with_email,
                "email_verified": leads_verified,
                "avg_score": round(float(avg_score), 1) if avg_score else None,
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
        elif any_cancelled and not any_launched:
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