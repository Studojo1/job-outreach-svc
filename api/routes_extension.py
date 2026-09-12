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
from typing import Any, Dict, List, Optional

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
    # The job's location. Without it, alternatives are unfiltered by city — a
    # Bengaluru student gets suggested companies anywhere in the world. My code
    # read getattr(request, "location") which was ALWAYS None because no model
    # declared it.
    location: Optional[str] = Field(default=None, max_length=255)
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
    # The job's location. Without it, alternatives are unfiltered by city — a
    # Bengaluru student gets suggested companies anywhere in the world. My code
    # read getattr(request, "location") which was ALWAYS None because no model
    # declared it.
    location: Optional[str] = Field(default=None, max_length=255)
    linkedin_url: Optional[str] = Field(default=None, max_length=2000)
    contact_email: Optional[str] = Field(default=None, max_length=320)
    # Off by default. A lookup costs an Apollo API call, and drafting happens
    # far more often than sending — so the preview answers from what we already
    # know unless the caller explicitly asks to spend one.
    allow_lookup: bool = False


class SimilarCompany(BaseModel):
    """A company we CAN reach, matched on the one the student clicked."""

    company: str
    contact_title: Optional[str] = None
    industry: Optional[str] = None


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
    # Populated ONLY when this company is unreachable. Same industry, size band
    # and role, and every one has a contact with a verified email — an
    # alternative we cannot email is the dead end we are escaping.
    # Advisory: nothing is drafted, redirected or sent for the student.
    similar: List["SimilarCompany"] = []


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


def _sends_today(db: Session, user_id: str) -> int:
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
        .join(Candidate, Lead.candidate_id == Candidate.id)
        .filter(
            # Scoped to the USER, across every candidate row they own.
            # Scoping to one candidate_id made the cap trivially resettable:
            # /candidate/upload creates a NEW Candidate on every upload, so
            # re-uploading a resume produced a fresh id with zero sends against
            # it. A limit that resets on demand is not a limit.
            Candidate.user_id == user_id,
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
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Who are we writing to?

    The page's own contact when it named one. Otherwise the best hiring contact
    we can find at that company — because "this page didn't name anyone" is a
    property of the job board, not a reason the student cannot reach the team.

    Returns None only when the company genuinely yields nobody.
    """
    from services.extension.contact_finder import looks_like_person

    page_name = (request.contact_name or "").strip()
    if page_name and not looks_like_person(page_name):
        # The page handed us something that is not a human. Ninjacart sent
        # "School alumni from Christ University, Bangalore" — a tile LinkedIn
        # renders under "People you can reach out to", scraped as the hiring
        # contact.
        #
        # Dropping it matters twice over. It never reaches a draft, AND the
        # branch below is skipped, so the Apollo search runs and finds the real
        # hiring manager. Accepting it did the opposite: it took the bad name
        # as final, never searched, and told the student nobody was named.
        logger.info(
            "[CONTACT-FIND] rejecting non-person contact_name %r for %s",
            page_name[:80], (request.company or "")[:60],
        )
        page_name = ""

    if page_name:
        return {
            "name": page_name,
            "title": request.contact_title,
            "linkedin_url": request.linkedin_url,
            "email": request.contact_email,
            "found_by_search": False,
        }

    # Reuse a contact we already found for this company — it saves an Apollo
    # search, and keeps the student writing to the same person across drafts
    # rather than a different stranger each time.
    # Searched across every candidate row this user owns, not just the current
    # one. /candidate/upload creates a NEW Candidate each time, so scoping to
    # one id meant a student who re-uploaded their resume lost every contact
    # they had already resolved — and we paid Apollo a second time for the
    # same person at the same company.
    prior_q = db.query(Lead).filter(
        Lead.company == request.company,
        Lead.status.in_(EXTENSION_LEAD_STATUSES),
        Lead.name.isnot(None),
    )
    if user_id:
        prior_q = prior_q.join(Candidate, Lead.candidate_id == Candidate.id).filter(
            Candidate.user_id == user_id
        )
    else:
        prior_q = prior_q.filter(Lead.candidate_id == candidate_id)
    prior = prior_q.order_by(Lead.id.desc()).first()
    # Rows written BEFORE the guard above existed can hold a bad name, and this
    # branch is checked ahead of the search — so without this check one bad
    # scrape would keep suppressing the lookup for that company forever.
    if prior and not looks_like_person((prior.name or "").strip()):
        logger.info(
            "[CONTACT-FIND] ignoring stored non-person lead name %r for %s",
            (prior.name or "")[:80], (request.company or "")[:60],
        )
        prior = None
    if prior and (prior.name or "").strip():
        return {
            "name": prior.name,
            "title": prior.title,
            "linkedin_url": prior.linkedin_url,
            "email": prior.email if prior.email_verified else None,
            "found_by_search": True,
        }

    from services.extension.contact_finder import find_hiring_contacts

    found = find_hiring_contacts(request.company, getattr(request, "role", None), limit=6)
    if not found:
        return None

    # Skip anyone we have ALREADY tried and failed to reveal. Apollo will not
    # find them on a retry, and returning the same dead contact is what made
    # the page say "no confirmed email for anyone at Sarvam" — when in truth
    # we had tried exactly one person and never looked at the other five the
    # search returned.
    tried_q = db.query(Lead.name).filter(
        Lead.company == request.company,
        Lead.status == "extension_no_email",
    )
    if user_id:
        tried_q = tried_q.join(Candidate, Lead.candidate_id == Candidate.id).filter(
            Candidate.user_id == user_id
        )
    else:
        tried_q = tried_q.filter(Lead.candidate_id == candidate_id)
    tried = {(n or "").strip().lower() for (n,) in tried_q.all()}

    best = next(
        (c for c in found if (c["name"] or "").strip().lower() not in tried),
        None,
    )
    if best is None:
        return None

    return {
        "name": best["name"],
        "title": best.get("title"),
        "linkedin_url": best.get("linkedin_url"),
        "email": best.get("email"),
        "apollo_id": best.get("apollo_id"),
        "company_domains": best.get("company_domains"),
        "found_by_search": True,
    }


def _reveal_belongs_to(email: Optional[str], company: str, domains: Optional[List[str]] = None) -> bool:
    """Did the reveal actually find someone AT this company?

    Apollo's people/match returns organization: None on every call — verified
    against Razorpay, Fractal, Zerodha and Swiggy — so there is nothing to
    compare, and it marks a NAME match email_status: "verified" regardless of
    employer. Searching "Sumit Kumar at Razorpay" returned
    sumit@razorcapital.net, a different company, and the verified flag passed
    it. A student would have emailed a stranger at a company they never
    applied to.

    Verified means the ADDRESS is real. It does not mean the PERSON is right.
    """
    try:
        from services.extension.contact_finder import email_matches_company

        # Prefer a resolved DOMAIN over a name. Names cannot separate a company
        # from its siblings: Apollo's own company search for "Bajaj Finance"
        # returns Bajaj Housing Finance and Bajaj Auto Finance, so every
        # recruiter it found was at bajajhousing.co.in and every reveal was
        # correctly rejected — which read to the student as "nobody works at
        # Bajaj Finance". Right rejection, wrong conclusion.
        return email_matches_company(email or "", company or "", domains)
    except Exception as e:
        logger.warning("[EXT-SEND] company check failed, refusing: %s", e)
        return False


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


def _reachable(
    contact: Dict[str, Any],
    message: str,
    cached: bool,
) -> "ContactCheckResponse":
    """We have an address: naming them is a promise we can keep."""
    return ContactCheckResponse(
        status="reachable",
        message=message,
        cached=cached,
        contact_name=contact["name"],
        contact_title=contact.get("title"),
        found_by_search=bool(contact.get("found_by_search")),
    )


def _suggest_alternatives(request: Any) -> List["SimilarCompany"]:
    """Companies like this one that we CAN email.

    Runs the outreach tool's own discovery — same LeadFilter, same
    build_apollo_query, same search — capped at a handful instead of 500. The
    student already told us what they want by clicking a specific job, which is
    the same signal the resume and quiz give the outreach tool.

    Only called when the clicked company is unreachable; a student with a
    working contact needs no alternative. Never raises: this runs on a page
    they are already reading.
    """
    try:
        from services.extension.similar_companies import find_similar_companies

        found = find_similar_companies(
            request.company,
            getattr(request, "role", None),
            getattr(request, "location", None),
        )
        return [
            SimilarCompany(
                company=c["company"],
                contact_title=c.get("contact_title"),
                industry=c.get("industry"),
            )
            for c in found
        ]
    except Exception as e:
        logger.warning("[SIMILAR] suggestion lookup failed: %s", e)
        return []


def _not_yet(
    message: str,
    cached: bool,
    status: str = "unreachable",
    similar: Optional[List["SimilarCompany"]] = None,
) -> "ContactCheckResponse":
    """We have no address, so we name NOBODY.

    Announcing "we found Santoshi — Recruiter at Joveo" and then being unable
    to send is worse than saying nothing: the student believes they have a
    contact, writes to that person in their head, and the tool cannot deliver
    it. A name is a promise that we can reach them. We only make it when we
    can keep it.

    The name is still stored on the lead — a later lookup may resolve the
    address, and then we can say it.
    """
    return ContactCheckResponse(
        status=status, message=message, cached=cached, similar=similar or []
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
    contact = _resolve_contact(db, candidate.id, request, current_user.id)
    if contact is None:
        return ContactCheckResponse(
            status="unreachable",
            # Grep [CONTACT-FIND] in the pod logs to see WHICH cause this was:
            # a refused Apollo call (HTTP 200 with a body error), no matches at
            # all, or matches with no verified address. From the outside those
            # looked identical, which is why this took four rounds to diagnose.
            message=(
                f"We haven't found anyone at {request.company} we can email yet. "
                "Your draft is saved and we keep looking."
            ),
            cached=True,
            similar=_suggest_alternatives(request),
        )
    if contact.get("email"):
        _log_resolution(current_user.id, request.company, "reachable", True, "search")
        return _reachable(contact, f"We can reach {contact['name']}.", True)

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
        return _reachable(contact, f"We can reach {contact['name']}.", True)

    # This PERSON was tried and failed. That is not the same as the company
    # being unreachable — _resolve_contact now skips anyone already marked
    # extension_no_email and returns the next candidate from the search, so
    # reaching here means either the page named this specific person, or every
    # alternate is exhausted too.
    if lead and lead.status == "extension_no_email":
        _log_resolution(current_user.id, request.company, "unreachable", True, "cache")
        return _not_yet(
            (
                f"We haven't found a confirmed email address at {request.company} yet. "
                "Your draft is saved and we keep looking."
            ),
            True,
            similar=_suggest_alternatives(request),
        )

    if not request.allow_lookup:
        # We found a PERSON but have not paid to reveal their address, so the
        # honest status is "unknown", not "unreachable" — we have not looked.
        #
        # But the alternatives still belong here. Pranav's instruction was that
        # when we cannot put an email in front of the student, we offer
        # companies we can reach. Until the reveal happens this draft has no
        # address either, which is the same dead end from where the student is
        # standing. Withholding the suggestions until someone presses "Check
        # now" meant they almost never appeared: the automatic check on page
        # load passes allow_lookup=false, so THIS is the branch nearly every
        # draft takes.
        #
        # The search that produced these is free; only the reveal costs.
        return _not_yet(
            "We'll look for an address when you send.",
            True,
            status="unknown",
            similar=_suggest_alternatives(request),
        )

    # Spend the lookup. The lead is committed first for the same reason as in
    # send-one: a rollback here would discard it and the next attempt would pay
    # Apollo again for the same person.
    if lead is None:
        lead = Lead(
            candidate_id=candidate.id,
            apollo_id=contact.get("apollo_id"),
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
    if result.success and _reveal_belongs_to((result.data or {}).get("email"), request.company, (contact or {}).get("company_domains")):
        lead.email = (result.data or {}).get("email")
        lead.email_verified = True
        db.commit()
        _log_resolution(current_user.id, request.company, "reachable", False, "apollo")
        return _reachable(contact, f"We can reach {contact['name']}.", False)

    lead.enrichment_fail_count = (lead.enrichment_fail_count or 0) + 1
    if result.error_type == "no_match":
        lead.status = "extension_no_email"
        db.commit()
        _log_resolution(current_user.id, request.company, "unreachable", False, "apollo")
        return _not_yet(
            (
                f"We haven't found a confirmed email address at {request.company} yet. "
                "Your draft is saved and we keep looking."
            ),
            False,
            similar=_suggest_alternatives(request),
        )

    # Transient — do not record it as unreachable, or a temporary outage would
    # permanently mark a findable person as a dead end.
    db.commit()
    _log_resolution(current_user.id, request.company, "error", False, result.error_type or "unknown")
    return _not_yet("We couldn't check just now. You can still write your email.", False, status="unknown")


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

    already = _sends_today(db, current_user.id)
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
    contact = _resolve_contact(db, candidate.id, request, current_user.id)
    if contact is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"no_contact_found: We haven't found anyone at {request.company} we can email yet. "
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
            apollo_id=contact.get("apollo_id"),
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
        revealed = (result.data or {}).get("email")
        if not _reveal_belongs_to(revealed, request.company, (contact or {}).get("company_domains")):
            logger.warning("[EXT-SEND] reveal rejected: %s is not at %s",
                           str(revealed)[:60], request.company[:40])
            lead.enrichment_fail_count = (lead.enrichment_fail_count or 0) + 1
            lead.status = "extension_no_email"
            db.commit()
            _log_resolution(current_user.id, request.company, "unreachable", False, "wrong_company")
            raise HTTPException(
                status_code=422,
                detail=("no_contact_email: We couldn't confirm an email address at "
                        f"{request.company} for this person. Your draft is saved."),
            )
        lead.email = revealed
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
    else:
        # COMMIT THE CHARGE ON ITS OWN, before anything else can fail.
        #
        # deduct_credits only mutates the session — routes_enrichment.py:225
        # commits immediately after calling it, for exactly this reason. Here
        # the charge shared a commit with the EmailSent insert at the end of
        # the function, so any failure in between rolled BOTH back: the email
        # had already left Gmail, and the student was neither charged nor
        # counted against their daily cap. Free, uncapped sends on any
        # transient database error.
        try:
            db.commit()
        except Exception as e:
            logger.error(
                "[EXT-SEND] Could not commit the credit charge for user %s: %s",
                current_user.id, e,
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
    # Separate from the charge above. If this fails the student has been
    # charged for an email that did leave — the right way round. Losing the
    # record costs them a cap slot; losing the charge costs us the send.
    try:
        db.commit()
    except Exception as e:
        logger.error("[EXT-SEND] Sent but could not record the send: %s", e)

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
