"""A person a user has already emailed is not a new lead.

Ticket #45: a student cancelled campaign 142 after three sends, started
campaign 145 on the same candidate, and it emailed the same three people
again, Prateek at Refyne twice. A new campaign took every lead the candidate
had, ordered by score, so the top of the list was always the people the last
campaign had just written to. 23 people across two users got the same
cold email twice this way, each one paid for with a credit.

The rule is per user, across all of their campaigns: once a first touch to an
address has gone out (or is going out), no later campaign sends that address
another first touch. Follow-ups in the original thread are unaffected, and
test emails do not count.

Two places apply it. create_campaign leaves contacted leads out, so they do
not take a slot in lead_limit; the worker checks again before every first
touch, because a restart, a replacement lead, or enrichment can still land
on an address that was already written to.
"""

from typing import Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.models import Campaign, Candidate, EmailSent

# A first touch in any of these states has reached, or is reaching, the person.
CONTACTED_STATUSES = ("sending", "sent", "replied", "bounced")

ALREADY_CONTACTED_MESSAGE = "Already emailed this person in an earlier campaign"


def _norm(address: Optional[str]) -> str:
    return (address or "").strip().lower()


def _first_touches(db: Session, user_id: str):
    return (
        db.query(EmailSent)
        .join(Campaign, Campaign.id == EmailSent.campaign_id)
        .join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(
            Candidate.user_id == user_id,
            func.coalesce(EmailSent.followup_number, 0) == 0,
            func.coalesce(EmailSent.is_test, False).is_(False),
            EmailSent.status.in_(CONTACTED_STATUSES),
        )
    )


def contacted(db: Session, user_id: str) -> tuple[Set[int], Set[str]]:
    """Lead ids and lowercased addresses this user has already sent a first touch to."""
    rows = _first_touches(db, user_id).with_entities(EmailSent.lead_id, EmailSent.to_email).all()
    lead_ids = {r.lead_id for r in rows if r.lead_id is not None}
    addresses = {_norm(r.to_email) for r in rows if r.to_email}
    return lead_ids, addresses


def already_contacted(db: Session, email: EmailSent) -> bool:
    """True if another campaign of the same user already sent a first touch to
    this email's address. Test emails and follow-ups are never blocked."""
    if email.is_test or (email.followup_number or 0) > 0 or not email.to_email:
        return False
    user_id = (
        db.query(Candidate.user_id)
        .join(Campaign, Campaign.candidate_id == Candidate.id)
        .filter(Campaign.id == email.campaign_id)
        .scalar()
    )
    if user_id is None:
        return False
    return db.query(
        _first_touches(db, user_id)
        .filter(
            EmailSent.id != email.id,
            func.lower(func.trim(EmailSent.to_email)) == _norm(email.to_email),
        )
        .exists()
    ).scalar()
