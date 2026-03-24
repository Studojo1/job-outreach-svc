"""Admin endpoints for outreach order monitoring dashboard."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from api.dependencies import get_admin_user
from database.models import (
    Campaign,
    EmailSent,
    Lead,
    OutreachOrder,
    PaymentOrder,
    User,
    UserCredit,
)
from database.session import get_db

router = APIRouter(prefix="/admin/outreach", tags=["admin"])

STUCK_THRESHOLD_HOURS = 6
ACTIVE_STATUSES = [
    "leads_generating", "leads_ready", "enriching", "enrichment_complete",
    "campaign_setup", "email_connected", "campaign_running",
]


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

    # Stuck orders
    stuck_orders = (
        db.query(func.count())
        .select_from(OutreachOrder)
        .filter(
            OutreachOrder.status.in_(ACTIVE_STATUSES),
            OutreachOrder.updated_at < stuck_cutoff,
        )
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

    return {
        "total_orders": total_orders,
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
    users = users_q.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

    result = []
    for u in users:
        # Orders
        orders = db.query(OutreachOrder).filter(OutreachOrder.user_id == u.id).all()
        active_order = next((o for o in orders if o.status in ACTIVE_STATUSES), None)

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
            and active_order.updated_at
            and active_order.updated_at.replace(tzinfo=timezone.utc) < stuck_cutoff
        )

        result.append({
            "user_id": u.id,
            "user_name": u.name,
            "user_email": u.email,
            "total_orders": len(orders),
            "active_order_status": active_order.status if active_order else None,
            "active_order_id": active_order.id if active_order else None,
            "active_order_updated_at": (
                active_order.updated_at.isoformat() if active_order and active_order.updated_at else None
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
            order.status in ACTIVE_STATUSES
            and order.updated_at
            and order.updated_at.replace(tzinfo=timezone.utc) < stuck_cutoff
        )

        orders_data.append({
            "id": order.id,
            "status": order.status,
            "leads_collected": order.leads_collected,
            "leads_target": order.leads_target,
            "is_stuck": is_stuck,
            "action_log": order.action_log or [],
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