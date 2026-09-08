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
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from database.session import get_db
from database.models import Candidate, EmailAccount, EmailSent, Lead, User

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

# Every status this endpoint sets. Used to scope lead reuse so we never touch a
# lead the campaign flow owns — those carry statuses like "new" or "contacted".
# Kill switch. Turning a feature off must not require a deploy — a deploy is
# slow exactly when you most need the feature stopped. Read per-request, so
# `kubectl set env` takes effect on the next call.
def _sends_disabled() -> bool:
    return os.getenv("EXTENSION_SEND_DISABLED", "").strip().lower() in {"1", "true", "yes"}


EXTENSION_LEAD_STATUSES = (
    "extension_pending",
    "extension_sent",
    "extension_no_email",
    "extension_lookup_failed",
)


class SendOneRequest(BaseModel):
    """Everything needed to send one email to one person."""

    # Who to write to. The extension scrapes these off the job page; all of it
    # is public. What it CANNOT see is the email address, which is the one
    # field Apollo is paid to return.
    # OPTIONAL — see ContactCheckRequest. When the page named nobody we find
    # the hiring contacts ourselves instead of refusing to send.
    contact_name: Optional[str] = Field(default=None, max_length=255)
    company: str = Field(min_length=1, max_length=255)
    contact_title: Optional[str] = Field(default=None, max_length=255)
    role: Optional[str] = Field(default=None, max_length=255)
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


class ContactCheckRequest(BaseModel):
    """Just enough to identify the person — no email content."""

    # OPTIONAL. A job page names someone maybe half the time; when it does
    # not, we search the company for whoever actually hires for this role
    # rather than calling it a dead end. Requiring a name here is what made
    # "no contact on the page" mean "you can never send this".
    contact_name: Optional[str] = Field(default=None, max_length=255)
    company: str = Field(min_length=1, max_length=255)
    contact_title: Optional[str] = Field(default=None, max_length=255)
    role: Optional[str] = Field(default=None, max_length=255)
    linkedin_url: Optional[str] = Field(default=None, max_length=2000)
    contact_email: Optional[str] = Field(default=None, max_length=320)
    # Off by default. A lookup costs an Apollo API call, and drafting happens
    # far more often than sending — so the preview answers from what we already
    # know unless the caller explicitly asks to spend one.
    allow_lookup: bool = False


class ContactCheckResponse(BaseModel):
    # "reachable" | "unreachable" | "unknown"
    status: str
    message: str
    # True when answered from stored data, so the caller knows nothing was spent.
    cached: bool
    # Set when we found the person ourselves rather than being given one.
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    found_by_search: bool = False


class SendOneResponse(BaseModel):
    sent: bool
    to_email: str
    credits_charged: int
    lead_id: int
    # Who it actually went to. When the page named nobody we found someone, and
    # the student must be told who rather than discovering it in their Sent
    # folder.
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    found_by_search: bool = False


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
    """How many extension emails this student has actually sent in 24h.

    Counted from EmailSent.sent_at, NOT from the Lead rows. A lead is reused
    when the student emails the same person twice, so it keeps its original
    created_at — counting leads meant a student re-contacting people saved last
    week saw a count of zero and walked straight past the cap.

    Extension sends are the rows with no campaign_id: they belong to no
    campaign, which is the entire point of this endpoint.
    """
    since = datetime.utcnow() - timedelta(days=1)
    return (
        db.query(EmailSent)
        .join(Lead, EmailSent.lead_id == Lead.id)
        .filter(
            Lead.candidate_id == candidate_id,
            EmailSent.campaign_id.is_(None),
            EmailSent.status == "sent",
            EmailSent.sent_at >= since,
        )
        .count()
    )


def _resolve_contact(
    db: Session,
    candidate_id: int,
    request: Any,
) -> Optional[Dict[str, Any]]:
    """Who are we writing to?

    The page's own contact when it named one. Otherwise the best hiring contact
    we can find at that company — because "this page didn't name anyone" is a
    property of the job board, not a reason the student cannot reach the team.

    Returns None only when the company genuinely yields nobody.
    """
    if (request.contact_name or "").strip():
        return {
            "name": request.contact_name.strip(),
            "title": request.contact_title,
            "linkedin_url": request.linkedin_url,
            "email": request.contact_email,
            "found_by_search": False,
        }

    # Reuse a contact we already found for this company — it saves an Apollo
    # search, and keeps the student writing to the same person across drafts
    # rather than a different stranger each time.
    prior = (
        db.query(Lead)
        .filter(
            Lead.candidate_id == candidate_id,
            Lead.company == request.company,
            Lead.status.in_(EXTENSION_LEAD_STATUSES),
            Lead.name.isnot(None),
        )
        .order_by(Lead.id.desc())
        .first()
    )
    if prior and (prior.name or "").strip():
        return {
            "name": prior.name,
            "title": prior.title,
            "linkedin_url": prior.linkedin_url,
            "email": prior.email if prior.email_verified else None,
            "found_by_search": True,
        }

    from services.extension.contact_finder import find_hiring_contacts

    found = find_hiring_contacts(request.company, getattr(request, "role", None), limit=3)
    if not found:
        return None
    best = found[0]
    return {
        "name": best["name"],
        "title": best.get("title"),
        "linkedin_url": best.get("linkedin_url"),
        "email": best.get("email"),
        "found_by_search": True,
    }


def _log_resolution(user_id: str, company: str, outcome: str, cached: bool, source: str) -> None:
    """One structured line per contact resolution.

    This is the number that decides whether the product works: if Apollo finds
    a verified address for most LinkedIn contacts, the flow is sound; if it
    finds few, students do all the work of editing an email that can never be
    sent. Nothing recorded that so far, so the hit rate was a guess.

    Greppable on purpose — `grep EXT-RESOLVE` over the pod logs answers it
    without a dashboard.
    """
    logger.info(
        "[EXT-RESOLVE] outcome=%s cached=%s source=%s company=%s user=%s",
        outcome, str(cached).lower(), source, (company or "")[:60], user_id,
    )


@router.post("/contact-check", response_model=ContactCheckResponse)
def check_contact(
    request: ContactCheckRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Can we actually reach this person?

    Asked at DRAFT time, so a student learns there is no address before
    writing an email rather than after. `no_contact_email` is the most likely
    failure of the whole flow — Apollo only returns verified addresses and
    rejects the guessed ones that bounce — and discovering it at the end,
    having already edited the message, is the worst possible moment.

    Free by default: answers from the page and from leads we already resolved.
    Only spends an Apollo call when the caller passes allow_lookup.
    """
    from services.enrichment.enrichment_service import enrich_single_lead_classified

    # The page itself gave us an address — nothing to resolve.
    if request.contact_email:
        _log_resolution(current_user.id, request.company, "reachable", True, "page")
        return ContactCheckResponse(
            status="reachable",
            message="We have an address for this person.",
            cached=True,
        )

    candidate = _resolve_candidate(db, current_user.id)
    if not candidate:
        return ContactCheckResponse(
            status="unknown",
            message="Add your resume and we'll check whether we can reach this person.",
            cached=True,
        )

    # Same resolution as send-one, so the preview tells the truth about who
    # the email will actually go to.
    contact = _resolve_contact(db, candidate.id, request)
    if contact is None:
        return ContactCheckResponse(
            status="unreachable",
            message=f"We couldn't find anyone at {request.company} to write to yet.",
            cached=True,
        )
    found_by_search = bool(contact.get("found_by_search"))

    if contact.get("email"):
        _log_resolution(current_user.id, request.company, "reachable", True, "search")
        return ContactCheckResponse(
            status="reachable",
            message=f"We can reach {contact['name']}.",
            cached=True,
            contact_name=contact["name"],
            contact_title=contact.get("title"),
            found_by_search=found_by_search,
        )

    lead = (
        db.query(Lead)
        .filter(
            Lead.candidate_id == candidate.id,
            Lead.name == contact["name"],
            Lead.company == request.company,
            Lead.status.in_(EXTENSION_LEAD_STATUSES),
        )
        .order_by(Lead.id.desc())
        .first()
    )

    # Already resolved once — reuse it rather than paying Apollo twice.
    if lead and lead.email and lead.email_verified:
        _log_resolution(current_user.id, request.company, "reachable", True, "cache")
        return ContactCheckResponse(
            status="reachable",
            message=f"We can reach {contact['name']}.",
            cached=True,
            contact_name=contact["name"],
            contact_title=contact.get("title"),
            found_by_search=found_by_search,
        )

    # Already tried and failed. Apollo will not find them on a retry, so say so
    # instead of spending another call to learn the same thing.
    if lead and lead.status == "extension_no_email":
        _log_resolution(current_user.id, request.company, "unreachable", True, "cache")
        return ContactCheckResponse(
            status="unreachable",
            message=f"We found {contact['name']}, but don't have a verified email address for them yet.",
            cached=True,
            contact_name=contact["name"],
            contact_title=contact.get("title"),
            found_by_search=found_by_search,
        )

    if not request.allow_lookup:
        return ContactCheckResponse(
            status="unknown",
            message=f"We'll look for {contact['name']}'s email when you send.",
            cached=True,
            contact_name=contact["name"],
            contact_title=contact.get("title"),
            found_by_search=found_by_search,
        )

    # Spend the lookup. The lead is committed first for the same reason as in
    # send-one: a rollback here would discard it and the next attempt would pay
    # Apollo again for the same person.
    if lead is None:
        lead = Lead(
            candidate_id=candidate.id,
            name=contact["name"],
            title=contact.get("title"),
            company=request.company,
            linkedin_url=contact.get("linkedin_url"),
            status="extension_pending",
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)

    result = enrich_single_lead_classified(lead)
    if result.success:
        lead.email = (result.data or {}).get("email")
        lead.email_verified = True
        db.commit()
        _log_resolution(current_user.id, request.company, "reachable", False, "apollo")
        return ContactCheckResponse(
            status="reachable",
            message=f"We can reach {contact['name']}.",
            cached=False,
            contact_name=contact["name"],
            contact_title=contact.get("title"),
            found_by_search=found_by_search,
        )

    lead.enrichment_fail_count = (lead.enrichment_fail_count or 0) + 1
    if result.error_type == "no_match":
        lead.status = "extension_no_email"
        db.commit()
        _log_resolution(current_user.id, request.company, "unreachable", False, "apollo")
        return ContactCheckResponse(
            status="unreachable",
            # Name them. Losing the name here is what made the page say
            # "we found Santoshi" and then "we'll find whoever hires for this
            # role" one click later — two contradictory claims about the same
            # draft, which reads as the tool not knowing what it is doing.
            message=f"We found {contact['name']}, but don't have a verified email address for them yet.",
            cached=False,
            contact_name=contact["name"],
            contact_title=contact.get("title"),
            found_by_search=found_by_search,
        )

    # Transient — do not record it as unreachable, or a temporary outage would
    # permanently mark a findable person as a dead end.
    db.commit()
    _log_resolution(current_user.id, request.company, "error", False, result.error_type or "unknown")
    return ContactCheckResponse(
        status="unknown",
        message="We couldn't check just now. You can still write your email.",
        cached=False,
        contact_name=contact["name"],
        contact_title=contact.get("title"),
        found_by_search=found_by_search,
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

    # Checked before anything else: when this is off, nothing is looked up,
    # nothing is charged, and nothing is written.
    if _sends_disabled():
        raise HTTPException(
            status_code=503,
            detail="send_paused: Sending is paused right now. Your draft is saved.",
        )

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
    # Only ever reuse a lead THIS endpoint created. The name+company match
    # would otherwise hit a lead discovered by the campaign flow, and the
    # status writes below would overwrite its campaign state — corrupting a
    # row this feature does not own, in a way no rollback here could undo.
    # A duplicate row is the cheaper mistake.
    # Who to write to. The page's contact, or the best hiring contact we can
    # find at the company — a page that names nobody is not a reason to give up.
    contact = _resolve_contact(db, candidate.id, request)
    if contact is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"no_contact_found: We couldn't find anyone at {request.company} to write to. "
                "Your draft is saved."
            ),
        )

    lead = (
        db.query(Lead)
        .filter(
            Lead.candidate_id == candidate.id,
            Lead.name == contact["name"],
            Lead.company == request.company,
            Lead.status.in_(EXTENSION_LEAD_STATUSES),
        )
        .order_by(Lead.id.desc())
        .first()
    )
    if lead is None:
        lead = Lead(
            candidate_id=candidate.id,
            name=contact["name"],
            title=contact.get("title"),
            company=request.company,
            linkedin_url=contact.get("linkedin_url"),
            email=str(contact["email"]) if contact.get("email") else None,
            email_verified=bool(contact.get("email")),
            status="extension_pending",
        )
        db.add(lead)
        # COMMIT before the Apollo call, not flush. Everything below can fail,
        # and a rollback would discard this row — so the next attempt would
        # insert a fresh lead and pay Apollo again for the same person. That is
        # worst for `no_match`: Apollo charges for a lookup that finds nothing,
        # so a student retrying a contact with no findable address would burn a
        # credit every time. Persisting first makes the dedupe above real.
        db.commit()
        db.refresh(lead)

    # ── Resolve the email address ────────────────────────────────────────
    # The only step that costs an Apollo credit, and it is skipped entirely
    # when we already hold a verified address for this person.
    if lead.email and lead.email_verified:
        _log_resolution(current_user.id, request.company, "reachable", True, "cache")
    else:
        result = enrich_single_lead_classified(lead)
        if not result.success:
            # Keep the lead and record what happened. enrichment_fail_count is
            # the column the enrichment flow already uses for this.
            lead.enrichment_fail_count = (lead.enrichment_fail_count or 0) + 1
            lead.status = "extension_no_email" if result.error_type == "no_match" else "extension_lookup_failed"
            db.commit()
            if result.error_type == "no_match":
                _log_resolution(current_user.id, request.company, "unreachable", False, "apollo")
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
            _log_resolution(current_user.id, request.company, "error", False, result.error_type or "unknown")
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
        _log_resolution(current_user.id, request.company, "reachable", False, "apollo")
        # Commit the address we just PAID Apollo for. If the send below fails
        # and this were rolled back, the retry would buy the same address a
        # second time.
        db.commit()

    to_email = lead.email
    if not to_email:
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
        # No rollback: the verified address is already paid for and worth
        # keeping. Nothing was charged to the student — the credit is deducted
        # further down, only on success.
        logger.error("[EXT-SEND] Gmail send raised for user %s: %s", current_user.id, e)
        raise HTTPException(
            status_code=502,
            detail="send_failed: Gmail rejected the message. Your draft is saved.",
        ) from e

    if not ok:
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

    # Record what went out. This is what the cap counts, and it gives the
    # student a durable record of the email rather than only a Lead status.
    # campaign_id stays NULL — this email belongs to no campaign, which is the
    # whole point of the endpoint.
    db.add(
        EmailSent(
            campaign_id=None,
            lead_id=lead.id,
            to_email=to_email,
            subject=request.subject,
            body=request.body,
            enrichment_status="enriched",
            status="sent",
            sent_at=datetime.utcnow(),
        )
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
        contact_name=lead.name,
        contact_title=lead.title,
        found_by_search=bool(contact.get("found_by_search")),
    )
