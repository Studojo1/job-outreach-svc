"""Where a user should go next, decided from what they actually have.

The rule this exists to enforce: a user who has paid, still holds enough
credits for a campaign, and has nothing running must always be sent to
Launch. Never back to resume upload, never to pricing.

Before this, every entry point decided on its own from order.status, and
order.status lies after payment: 6 paying users (Rs 9,563) sat for up to
four months without a campaign. One was at 'created' with a linked payment
and got sent back to upload; one paid Rs 3,465, sent her test emails, then
followed the landing page's "Find My Hiring Managers" button and re-uploaded
her resume three times without ever seeing Launch again.

The answer is derived from credits, campaigns, candidates and mailboxes,
never from order.status, so no stale status can misroute a paid user.
Paths are relative to /outreach, like /orders/{id}/resume's.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, Candidate, EmailAccount, Lead, OutreachOrder, UserCredit
from services.credits import MIN_CAMPAIGN_CREDITS
from services.stage_tracking import promote_paid_order

logger = get_logger(__name__)

# States
CAMPAIGN_ACTIVE = "campaign_active"   # running or paused: the dashboard is home
LAUNCH_DRAFT = "launch_draft"         # a campaign was created but never sent
LAUNCH_READY = "launch_ready"         # paid, profile + leads + Gmail ready: Launch
CONNECT_GMAIL = "connect_gmail"       # paid, profile + leads, no mailbox
NEEDS_PROFILE = "needs_profile"       # paid, but no finished profile with leads
NOT_PAID = "not_paid"                 # no spendable credits: normal funnel applies

# States in which the user has paid and has not launched. Every entry point
# must route these to `path`; the launch-stall sweep nudges them.
PAID_NOT_LAUNCHED = (LAUNCH_DRAFT, LAUNCH_READY, CONNECT_GMAIL, NEEDS_PROFILE)


@dataclass
class NextStep:
    state: str
    path: Optional[str]
    available_credits: int
    order_id: Optional[int] = None
    candidate_id: Optional[int] = None
    email_account_id: Optional[int] = None
    campaign_id: Optional[int] = None
    # True once any campaign has actually been launched. A returning customer
    # holding credits (a finished campaign handed back its unsent work) gets
    # "launch another campaign", not "nothing has been sent yet".
    has_launched: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _user_campaigns(db: Session, user_id: str):
    return (
        db.query(Campaign)
        .join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(Candidate.user_id == user_id)
        .order_by(Campaign.created_at.desc())
        .all()
    )


def _launchable_candidate(db: Session, user_id: str, preferred: Optional[int]) -> Optional[int]:
    """A candidate with a finished profile and leads: the order's own if it
    qualifies, else the user's most recent one that does (same rule as
    routes_campaign._resolve_effective_candidate, which /campaign/create
    applies again at launch)."""
    rows = (
        db.query(Candidate.id, Candidate.parsed_json, func.count(Lead.id))
        .outerjoin(Lead, Lead.candidate_id == Candidate.id)
        .filter(Candidate.user_id == user_id)
        .group_by(Candidate.id)
        .order_by(Candidate.created_at.desc())
        .all()
    )
    ok = [cid for cid, parsed, leads in rows
          if leads and parsed and parsed.get("career_analysis")]
    if preferred in ok:
        return preferred
    return ok[0] if ok else None


def resolve_next_step(db: Session, user_id: str, *, heal: bool = True) -> NextStep:
    """Decide where the user goes next. With heal=True, also repair the active
    order a paid user is stuck behind (status, mailbox link) and commit."""
    wallet = db.query(UserCredit).filter_by(user_id=user_id).first()
    available = (wallet.total_credits - wallet.used_credits) if wallet else 0

    order = (
        db.query(OutreachOrder)
        .filter(OutreachOrder.user_id == user_id, OutreachOrder.status != "completed")
        .order_by(OutreachOrder.created_at.desc())
        .first()
    )
    order_id = order.id if order else None

    campaigns = _user_campaigns(db, user_id)
    has_launched = any(c.status != "draft" for c in campaigns)
    active = next((c for c in campaigns if c.status in ("running", "paused")), None)
    if active is not None:
        return NextStep(CAMPAIGN_ACTIVE, "/campaign/dashboard", available,
                        order_id=order_id, campaign_id=active.id,
                        candidate_id=active.candidate_id,
                        email_account_id=active.email_account_id, has_launched=has_launched)

    # A draft holds its credits already, so the wallet can read 0 here. It
    # needs /send, not a second /create (which would reserve twice).
    draft = next((c for c in campaigns if c.status == "draft"), None)
    if draft is not None:
        return NextStep(LAUNCH_DRAFT, "/campaign/setup", available,
                        order_id=order_id, campaign_id=draft.id,
                        candidate_id=draft.candidate_id,
                        email_account_id=draft.email_account_id, has_launched=has_launched)

    if available < MIN_CAMPAIGN_CREDITS:
        return NextStep(NOT_PAID, None, available, order_id=order_id, has_launched=has_launched)

    # LinkedIn-only plans launch through their own flow.
    if order is not None and (getattr(order, "plan_type", None) or "email") == "linkedin":
        return NextStep(NOT_PAID, None, available, order_id=order_id, has_launched=has_launched)

    candidate_id = _launchable_candidate(db, user_id, order.candidate_id if order else None)
    mailbox = (
        db.query(EmailAccount)
        .filter_by(user_id=user_id, provider="gmail")
        .order_by(EmailAccount.created_at.desc())
        .first()
    )

    if candidate_id is None:
        step = NextStep(NEEDS_PROFILE, "/onboarding/upload", available, order_id=order_id,
                        email_account_id=mailbox.id if mailbox else None, has_launched=has_launched)
    elif mailbox is None:
        step = NextStep(CONNECT_GMAIL, "/connect/gmail", available, order_id=order_id,
                        candidate_id=candidate_id, has_launched=has_launched)
    else:
        step = NextStep(LAUNCH_READY, "/campaign/setup", available, order_id=order_id,
                        candidate_id=candidate_id, email_account_id=mailbox.id, has_launched=has_launched)

    if heal and order is not None:
        changed = promote_paid_order(order, "next-step: paid with credits, no campaign")
        if mailbox is not None and not order.email_account_id:
            order.email_account_id = mailbox.id
            order.updated_at = datetime.utcnow()
            changed = True
        if changed:
            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("[NEXT-STEP] could not heal order %s", order.id)

    return step
