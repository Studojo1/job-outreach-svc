"""Credits — the one place user_credits changes.

Every grant, reservation and release goes through here and writes a
credit_ledger row in the same transaction (migration 049), so for any user
the question "where did my credits go" has an answer. Before this, the
wallet was a bare total/used pair mutated from six call sites and nothing
could explain a balance (audit P11, P29).

Vocabulary:
  grant    total_credits += n   a payment, coupon or admin comp
  reserve  used_credits  += n   credits set aside for a campaign / enrichment / send
  release  used_credits  -= n   a reservation given back (failed send, unsent
                                 work on a finished campaign, failed create)

Callers own the transaction: nothing here commits. That keeps the balance
change and whatever caused it (a paid order, a failed email row) atomic.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, CreditLedger, UserCredit

logger = get_logger(__name__)


# The smallest plan. /campaign/create refuses to start below it, and a paid
# user holding at least this much with nothing running is "paid, not launched".
MIN_CAMPAIGN_CREDITS = 50


# Reasons. Short and stable: the admin panel and reconciliation group by them.
GRANT_PAYMENT = "grant_payment"
GRANT_COUPON = "grant_coupon"
GRANT_ADMIN = "grant_admin"
RESERVE_CAMPAIGN = "reserve_campaign"
RESERVE_ENRICHMENT = "reserve_enrichment"
RESERVE_EXTENSION_SEND = "reserve_extension_send"
RELEASE_CREATE_FAILED = "release_create_failed"
RELEASE_CAMPAIGN_CANCELLED = "release_campaign_cancelled"
RELEASE_CAMPAIGN_FINISHED = "release_campaign_finished"
RELEASE_SEND_FAILED = "release_send_failed"
RELEASE_ENRICHMENT_UNUSED = "release_enrichment_unused"
RELEASE_ADMIN = "release_admin"
REVOKE_REFUND = "revoke_refund"


def lock_wallet(db: Session, user_id: str) -> Optional[UserCredit]:
    """The user's wallet row, locked for the rest of the transaction."""
    return db.query(UserCredit).filter_by(user_id=user_id).with_for_update().first()


def available(db: Session, user_id: str) -> int:
    wallet = db.query(UserCredit).filter_by(user_id=user_id).first()
    return (wallet.total_credits - wallet.used_credits) if wallet else 0


def _record(db: Session, user_id: str, *, delta_total: int = 0, delta_used: int = 0,
            reason: str, campaign_id: Optional[int] = None,
            payment_order_id: Optional[int] = None, actor: Optional[str] = None,
            note: Optional[str] = None) -> CreditLedger:
    entry = CreditLedger(
        user_id=user_id,
        delta_total=delta_total,
        delta_used=delta_used,
        reason=reason,
        campaign_id=campaign_id,
        payment_order_id=payment_order_id,
        actor=actor,
        note=note,
        created_at=datetime.utcnow(),
    )
    db.add(entry)
    return entry


def grant(db: Session, user_id: str, amount: int, reason: str, *,
          payment_order_id: Optional[int] = None, actor: Optional[str] = None,
          note: Optional[str] = None) -> int:
    """Add purchased or comped credits. Creates the wallet if needed."""
    if amount <= 0:
        return 0
    wallet = lock_wallet(db, user_id)
    if wallet is None:
        wallet = UserCredit(user_id=user_id, total_credits=0, used_credits=0)
        db.add(wallet)
    wallet.total_credits += amount
    wallet.updated_at = datetime.utcnow()
    _record(db, user_id, delta_total=amount, reason=reason,
            payment_order_id=payment_order_id, actor=actor, note=note)
    return amount


def revoke(db: Session, user_id: str, amount: int, reason: str, *,
           payment_order_id: Optional[int] = None, actor: Optional[str] = None,
           note: Optional[str] = None) -> int:
    """Take purchased credits back (a refunded payment). Never below what is
    already in use: returns how many were actually revoked."""
    if amount <= 0:
        return 0
    wallet = lock_wallet(db, user_id)
    if wallet is None:
        return 0
    amount = min(amount, wallet.total_credits - wallet.used_credits)
    if amount <= 0:
        return 0
    wallet.total_credits -= amount
    wallet.updated_at = datetime.utcnow()
    _record(db, user_id, delta_total=-amount, reason=reason,
            payment_order_id=payment_order_id, actor=actor, note=note)
    return amount


def reserve(db: Session, user_id: str, amount: int, reason: str, *,
            campaign: Optional[Campaign] = None, actor: Optional[str] = None,
            note: Optional[str] = None) -> Optional[CreditLedger]:
    """Set credits aside. Returns the ledger row, or None (and no change) if
    the balance is short.

    Takes the wallet row lock, so two concurrent reservations cannot both
    pass the balance check (the gap deduct_credits had, audit P31).
    """
    if amount <= 0:
        raise ValueError(f"reserve amount must be positive, got {amount}")
    wallet = lock_wallet(db, user_id)
    if wallet is None or (wallet.total_credits - wallet.used_credits) < amount:
        return None
    wallet.used_credits += amount
    wallet.updated_at = datetime.utcnow()
    if campaign is not None:
        campaign.credits_reserved = (campaign.credits_reserved or 0) + amount
    return _record(db, user_id, delta_used=amount, reason=reason,
                   campaign_id=campaign.id if campaign is not None else None,
                   actor=actor, note=note)


def release(db: Session, user_id: str, amount: int, reason: str, *,
            campaign: Optional[Campaign] = None, actor: Optional[str] = None,
            note: Optional[str] = None) -> int:
    """Give reserved credits back. Returns how many were actually released.

    Clamped twice so a bad caller cannot mint credits:
      - never below zero used_credits on the wallet;
      - for a campaign, never more than it still holds
        (credits_reserved - credits_released), when that is known.
    """
    if amount <= 0:
        return 0
    if campaign is not None and campaign.credits_reserved is not None:
        amount = min(amount, campaign.credits_reserved - (campaign.credits_released or 0))
    wallet = lock_wallet(db, user_id)
    if wallet is None:
        return 0
    amount = min(amount, wallet.used_credits)
    if amount <= 0:
        return 0
    wallet.used_credits -= amount
    wallet.updated_at = datetime.utcnow()
    if campaign is not None:
        campaign.credits_released = (campaign.credits_released or 0) + amount
    _record(db, user_id, delta_used=-amount, reason=reason,
            campaign_id=campaign.id if campaign is not None else None,
            actor=actor, note=note)
    logger.info("[CREDITS] released %d to %s (%s, campaign=%s)",
                amount, user_id, reason, campaign.id if campaign is not None else None)
    return amount


def attach_campaign(entry: CreditLedger, campaign: Campaign) -> None:
    """Point a reservation taken before the campaign row existed at it.

    /campaign/create has to reserve before create_campaign runs (so a
    concurrent request sees the lower balance), which is before there is a
    campaign id to record.
    """
    entry.campaign_id = campaign.id
    campaign.credits_reserved = (campaign.credits_reserved or 0) + entry.delta_used
