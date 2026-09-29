"""The order <-> campaign link, one order to many campaigns (audit P03).

outreach_orders.campaign_id is a single pointer: a second campaign overwrote
it and the first one vanished from every view while it kept sending. The
authoritative link is now campaigns.outreach_order_id (migration 049), which
a later campaign cannot overwrite. The old pointer is no longer written; it
is only read as a fallback for campaigns created before the link existed.
"""

from typing import Optional

from sqlalchemy.orm import Session

from database.models import Campaign, OutreachOrder


def order_for_campaign(db: Session, campaign: Optional[Campaign]) -> Optional[OutreachOrder]:
    if campaign is None:
        return None
    if campaign.outreach_order_id:
        order = db.get(OutreachOrder, campaign.outreach_order_id)
        if order is not None:
            return order
    return (
        db.query(OutreachOrder).filter(OutreachOrder.campaign_id == campaign.id)
        .order_by(OutreachOrder.id.desc()).first()
    )


def campaigns_for_order(db: Session, order: OutreachOrder) -> list:
    """Newest first. Includes a legacy campaign reached only by the old pointer."""
    rows = (
        db.query(Campaign).filter(Campaign.outreach_order_id == order.id)
        .order_by(Campaign.created_at.desc()).all()
    )
    if order.campaign_id and all(c.id != order.campaign_id for c in rows):
        legacy = db.get(Campaign, order.campaign_id)
        if legacy is not None:
            rows.append(legacy)
    return rows


def current_campaign_id(db: Session, order: OutreachOrder) -> Optional[int]:
    """The campaign My Orders and the dashboard should open for this order:
    a running or paused one first, else the newest. With two campaigns on one
    order, the newest-only rule hid a still-running older one (audit PS-N02)."""
    rows = campaigns_for_order(db, order)
    live = next((c for c in rows if c.status in ("running", "paused")), None)
    if live is not None:
        return live.id
    return rows[0].id if rows else None


def link(campaign: Campaign, order: Optional[OutreachOrder]) -> None:
    """Attach a campaign to an order if it has none. Caller commits."""
    if order is not None and campaign is not None and not campaign.outreach_order_id:
        campaign.outreach_order_id = order.id
