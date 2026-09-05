"""Extension outreach — send ONE email to ONE person.

Why this exists
---------------
The campaign system cannot do this, and that is not a bug in it — it was built
for a different job. Three things make it unusable for the extension:

  1. ``create_campaign`` selects recipients with
     ``Lead.candidate_id == candidate_id`` ordered by score
     (services/email_campaign/campaign_service.py:104). There is no parameter
     naming a recipient, so ``lead_limit=1`` sends to the student's top-scored
     EXISTING lead — a stranger, not the contact on the job page they just
     opened.
  2. No PATCH/PUT on an email exists anywhere in routes_campaign.py, so a
     student's edited text cannot be written back. ``/campaign/create`` always
     AI-generates: blank ``selected_styles`` defaults to
     ``["warm_intro","value_prop"]`` (routes_campaign.py:258-260), which makes
     the ``subject_template``/``body_template`` branch unreachable.
  3. ``MIN_CAMPAIGN_CREDITS = 50`` gates entry to a flow that would then do the
     wrong two things above.

So this router does the one narrow thing the extension needs, reusing the
service's own machinery rather than reimplementing it:

  * Apollo people/match, via ``enrich_single_lead_classified`` — the same
    lookup enrichment uses, including its verified-only rule that rejects
    guessed addresses which bounce and wreck sender reputation.
  * ``send_email_via_gmail`` — a finished helper in gmail_send_service.py with
    ZERO callers before this one. The campaign worker uses a different
    function (``send_gmail_email``) and is untouched by this file.
  * ``deduct_credits`` — the shared helper routes_enrichment and
    routes_campaign already import.

NOTHING in the campaign path is read, called, or modified here.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from database.session import get_db
from database.models import Candidate, EmailAccount, Lead, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extension", tags=["extension"])

# One credit per email that actually leaves. Drafting, editing and looking up a
# contact are all free — the student is only charged when we hand a message to
# Gmail.
CREDITS_PER_SEND = 1

# A bug in the extension must not be able to drain a student's wallet or the
# shared Apollo quota before anyone notices. This is a blast-radius limit, not
# a product decision.
DAILY_SEND_CAP = 25


class SendOneRequest(BaseModel):
    """Everything needed to send one email to one person."""

    # Who to write to. The extension scrapes these off the job page; all of it
    # is public. What it CANNOT see is the email address, which is the one
    # field Apollo is paid to return.
    contact_name: str = Field(min_length=1, max_length=255)
    company: str = Field(min_length=1, max_length=255)
    contact_title: Optional[str] = Field(default=None, max_length=255)
    linkedin_url: Optional[str] = Field(default=None, max_length=2000)
    # When a page did expose an address, skip the lookup entirely and save the
    # Apollo call. Deliberately a plain str: pydantic's EmailStr needs the
    # email-validator package, which is NOT in requirements.txt and is used
    # nowhere else in this service — importing it would raise at startup and
    # take the whole container down, campaigns included.
    contact_email: Optional[str] = Field(default=None, max_length=320)

    # The student's own words, exactly as they left the CRM editor. Nothing
    # regenerates these.
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)

    # Which mailbox to send from. Resolved server-side when absent.
    email_account_id: Optional[int] = None


class SendOneResponse(BaseModel):
    sent: bool
    to_email: str
    credits_charged: int
    lead_id: int


def _resolve_candidate(db: Session, user_id: str) -> Optional[Candidate]:
    """The student's active candidate row.

    Newest wins: uploading a resume creates a new Candidate rather than
    updating the old one, so the most recent is the one their current profile
    lives on.
    """
    return (
        db.query(Candidate)
        .filter(Candidate.user_id == user_id)
        .order_by(Candidate.created_at.desc(), Candidate.id.desc())
        .first()
    )


def _resolve_email_account(
    db: Session, user_id: str, requested_id: Optional[int]
) -> EmailAccount:
    """The mailbox to send from, always scoped to this user.

    A client-supplied account id is verified against the session's own user
    before use — otherwise it would be a way to send from someone else's
    mailbox.
    """
    q = db.query(EmailAccount).filter(EmailAccount.user_id == user_id)
    if requested_id is not None:
        account = q.filter(EmailAccount.id == requested_id).first()
        if not account:
            raise HTTPException(
                status_code=404, detail="That Gmail account is not connected to your profile."
            )
    else:
        account = q.order_by(EmailAccount.created_at.desc()).first()

    if not account:
        raise HTTPException(
            status_code=409,
            detail="needs_gmail: Connect Gmail so this sends from your own address.",
        )
    if not account.access_token:
        raise HTTPException(
            status_code=409,
            detail="needs_gmail: Your Gmail connection expired. Reconnect it to send.",
        )
    return account


def _sends_today(db: Session, candidate_id: int) -> int:
    """How many extension emails this student has already sent today.

    Counted from the Lead rows this endpoint creates, which is the only record
    of extension sends. ``status`` is set to "extension_sent" precisely so this
    query can find them without touching campaign data.
    """
    since = datetime.utcnow() - timedelta(days=1)
    return (
        db.query(Lead)
        .filter(
            Lead.candidate_id == candidate_id,
            Lead.status == "extension_sent",
            Lead.created_at >= since,
        )
        .count()
    )


@router.post("/send-one", response_model=SendOneResponse)
def send_one_email(
    request: SendOneRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send the student's edited email to the contact from the job page.

    Order matters here. Every free check runs before anything that costs money,
    and the credit is only spent after Gmail confirms the send — so a failure
    at any point leaves the student's balance untouched.
    """
    from api.routes_payment import deduct_credits
    from services.enrichment.enrichment_service import enrich_single_lead_classified
    from services.email_campaign.gmail_send_service import send_email_via_gmail

    # ── Free checks first ────────────────────────────────────────────────
    candidate = _resolve_candidate(db, current_user.id)
    if not candidate:
        raise HTTPException(
            status_code=409,
            detail="needs_profile: Add your resume so we know what to say about you.",
        )

    account = _resolve_email_account(db, current_user.id, request.email_account_id)

    already = _sends_today(db, candidate.id)
    if already >= DAILY_SEND_CAP:
        raise HTTPException(
            status_code=429,
            detail=f"You've sent {already} emails today. The daily limit is {DAILY_SEND_CAP} — try again tomorrow.",
        )

    # Check the balance BEFORE spending an Apollo credit on a lookup the
    # student cannot afford to use.
    from database.models import UserCredit

    credit_row = db.query(UserCredit).filter_by(user_id=current_user.id).first()
    available = (credit_row.total_credits - credit_row.used_credits) if credit_row else 0
    if available < CREDITS_PER_SEND:
        raise HTTPException(
            status_code=402,
            detail=f"needs_credits: Sending one email costs {CREDITS_PER_SEND} credit and you have {available}.",
        )

    # ── The lead row ─────────────────────────────────────────────────────
    # A Lead is how this service represents "a person you might email", and
    # both Apollo enrichment and the send path expect one. Reusing an existing
    # row for the same person avoids paying Apollo twice for one contact.
    lead = (
        db.query(Lead)
        .filter(
            Lead.candidate_id == candidate.id,
            Lead.name == request.contact_name,
            Lead.company == request.company,
        )
        .order_by(Lead.id.desc())
        .first()
    )
    if lead is None:
        lead = Lead(
            candidate_id=candidate.id,
            name=request.contact_name,
            title=request.contact_title,
            company=request.company,
            linkedin_url=request.linkedin_url,
            email=str(request.contact_email) if request.contact_email else None,
            email_verified=bool(request.contact_email),
            status="extension_pending",
        )
        db.add(lead)
        db.flush()

    # ── Resolve the email address ────────────────────────────────────────
    # The only step that costs an Apollo credit, and it is skipped entirely
    # when we already hold a verified address for this person.
    if not (lead.email and lead.email_verified):
        result = enrich_single_lead_classified(lead)
        if not result.success:
            db.rollback()
            if result.error_type == "no_match":
                raise HTTPException(
                    status_code=422,
                    detail="no_contact_email: We couldn't find a verified email for this person. Your draft is saved.",
                )
            if result.error_type == "credit_exhausted":
                logger.error("[EXT-SEND] Apollo credits exhausted: %s", result.error_detail[:200])
                raise HTTPException(
                    status_code=503,
                    detail="lookup_unavailable: We can't look up contacts right now. Your draft is saved — try again shortly.",
                )
            logger.warning(
                "[EXT-SEND] Apollo lookup failed (%s): %s",
                result.error_type, result.error_detail[:200],
            )
            raise HTTPException(
                status_code=503,
                detail="lookup_failed: Couldn't reach the contact lookup service. Your draft is saved — try again shortly.",
            )
        lead.email = (result.data or {}).get("email")
        lead.email_verified = True
        db.flush()

    to_email = lead.email
    if not to_email:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="no_contact_email: We couldn't find a verified email for this person. Your draft is saved.",
        )

    # ── Send ─────────────────────────────────────────────────────────────
    # The student's exact subject and body. Nothing is regenerated, nothing is
    # templated — this is the whole reason the endpoint exists.
    try:
        ok = send_email_via_gmail(
            to_email=to_email,
            subject=request.subject,
            body=request.body,
            email_account_id=account.id,
        )
    except Exception as e:
        db.rollback()
        logger.error("[EXT-SEND] Gmail send raised for user %s: %s", current_user.id, e)
        raise HTTPException(
            status_code=502,
            detail="send_failed: Gmail rejected the message. Your draft is saved.",
        )

    if not ok:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail="send_failed: Gmail didn't accept the message. Your draft is saved.",
        )

    # ── Charge, only now that it actually went ───────────────────────────
    if not deduct_credits(db, current_user.id, CREDITS_PER_SEND):
        # The balance was checked above, so this is a race with another send.
        # The email is already gone; log it and let the student keep it rather
        # than reporting a failure for something that succeeded.
        logger.error(
            "[EXT-SEND] Email sent but credit deduction failed for user %s — not charged.",
            current_user.id,
        )

    lead.status = "extension_sent"
    db.commit()

    logger.info(
        "[EXT-SEND] user=%s lead=%d company=%s sent",
        current_user.id, lead.id, request.company,
    )
    return SendOneResponse(
        sent=True,
        to_email=to_email,
        credits_charged=CREDITS_PER_SEND,
        lead_id=lead.id,
    )
