"""Profile, status, greeting and the default chips: the second half of
services/agent/reads.py, re-exported from there. Import these through
`services.agent.reads`.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import EmailAccount, EmailSent, User, UserCredit
from services.agent.context import first_name_of, not_found, resolve_agent_candidate, user_id_of
from services.agent.reads import (
    MAX_MESSAGE_CHARS,
    NOTE_KEYS,
    _candidate_or_none,
    _counts,
    _outreach_rows,
    _touch1_query,
    cap,
    match_totals,
    usage_today,
)
from services.agent.schemas import Credits, GmailState, Limits, MatchTotals, Profile, ProfileNotes, Status
from services.agent.schemas_chat import Suggestion

logger = get_logger(__name__)


def _strs(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    out = []
    for v in value or []:
        if isinstance(v, dict):
            v = v.get("title") or v.get("name")
        if isinstance(v, str) and v.strip() and v.strip() not in out:
            out.append(v.strip())
    return out[:limit]


def _user(db: Session, uid: str) -> Optional[User]:
    return db.query(User).filter(User.id == uid).first()


def profile(db: Session, user: Any, candidate: Any = None) -> Profile:
    from services.candidate_intelligence.payload_builder import compute_hiring_manager_titles
    from services.email_campaign.email_generator_service import extract_candidate_profile

    uid = user_id_of(user)
    cand = _candidate_or_none(db, uid, candidate)
    if cand is None:
        raise not_found("Upload your resume first, then I can read your profile.")
    account = (getattr(_user(db, uid), "name", None) or "").strip()
    try:
        ep = extract_candidate_profile(cand, fallback_name=account)
    except Exception:
        logger.warning("[AGENT] profile extraction failed for candidate %s", cand.id, exc_info=True)
        ep = {}
    parsed = cand.parsed_json if isinstance(cand.parsed_json, dict) else {}
    prefs = parsed.get("preferences") if isinstance(parsed.get("preferences"), dict) else {}
    rp = cand.resume_profile if isinstance(cand.resume_profile, dict) else {}
    roles = _strs(cand.target_roles) or _strs((parsed.get("career_analysis") or {}).get("recommended_roles"))
    try:
        titles = compute_hiring_manager_titles(roles, rp)
    except Exception:
        titles = []
    flex = cand.flex_notes if isinstance(cand.flex_notes, dict) else {}
    summary = parsed.get("profile_summary") or rp.get("summary")
    stage = prefs.get("career_stage") or rp.get("career_stage") or rp.get("seniority")
    return Profile(
        candidate_id=cand.id, name=ep.get("candidate_name") or account or "You",
        education=ep.get("education") or None,
        summary=summary.strip()[:600] if isinstance(summary, str) and summary.strip() else None,
        top_skills=_strs(rp.get("top_skills")) or _strs(ep.get("key_skills")),
        target_roles=roles, target_industries=_strs(cand.target_industries),
        dream_companies=_strs(cand.dream_companies), locations=_strs(prefs.get("locations")),
        career_stage=stage if isinstance(stage, str) and stage else None,
        hiring_manager_titles=_strs(titles),
        notes=ProfileNotes(**{k: (flex.get(k) or None) if isinstance(flex.get(k), str) else None
                              for k in NOTE_KEYS}),
    )


def _credits(db: Session, uid: str) -> Credits:
    """The numbers GET /payment/credits reports as available and reserved."""
    w = db.query(UserCredit).filter(UserCredit.user_id == uid).first()
    if w is None:
        return Credits(available=0, reserved=0)
    delivered = (_touch1_query(db, uid).filter(EmailSent.status == "sent").count())
    return Credits(available=max(0, w.total_credits - w.used_credits), reserved=max(0, w.used_credits - delivered))


def _gmail(db: Session, uid: str) -> GmailState:
    box = (db.query(EmailAccount).filter(EmailAccount.user_id == uid, EmailAccount.provider == "gmail",
                                         EmailAccount.access_token != "")
           .order_by(EmailAccount.created_at.desc()).first())
    if box is None:
        return GmailState(connected=False)
    return GmailState(connected=True, address=box.email_address, token_valid=box.token_invalid_at is None)


def status(db: Session, user: Any) -> Status:
    """Where things stand. `gmail.address` is for the UI only; tools drop it."""
    uid = user_id_of(user)
    first = ""
    u = _user(db, uid)
    if u is not None:
        first = first_name_of(u.name)
    cand = resolve_agent_candidate(db, uid)
    state = "needs_resume"
    if cand is not None:
        try:
            from services.next_step import resolve_next_step
            # A savepoint, so a failure here never discards the caller's pending work.
            with db.begin_nested():
                state = resolve_next_step(db, uid, heal=False).state
        except Exception as e:
            logger.warning("[AGENT] next-step state failed: %s", type(e).__name__)
            state = "not_paid"
    rows = _outreach_rows(db, uid)
    usage = usage_today(db, uid)
    waiting = sum(1 for r in rows if r.kind == "draft" and r.server_status in ("drafting", "needs_review"))
    return Status(
        state=state, candidate_id=cand.id if cand is not None else None, first_name=first,
        credits=_credits(db, uid), gmail=_gmail(db, uid), counts=_counts(rows),
        matches=match_totals(db, uid, cand) if cand is not None else MatchTotals(total=0, strong=0, new=0),
        limits=Limits(
            messages_today=usage["messages"], messages_cap=cap("AGENT_DAILY_MESSAGE_CAP", 60),
            drafts_today=usage["drafts"], drafts_cap=cap("AGENT_DAILY_DRAFT_CAP", 30),
            drafts_waiting=waiting, drafts_waiting_cap=cap("AGENT_MAX_WAITING_DRAFTS", 10),
            max_message_chars=MAX_MESSAGE_CHARS),
    )


def greeting(db: Session, user: Any, st: Optional[Status] = None) -> str:
    """One line built from real counts, no model call."""
    st = st or status(db, user)
    hi = f"Hi {st.first_name}." if st.first_name else "Hi."
    if st.state == "needs_resume":
        return f"{hi} Upload your resume and I can start on the people to write to."
    if st.matches.total == 0:
        return f"{hi} Your matches are not ready yet. Check back once your search finishes."
    parts = [f"{hi} You have {st.matches.new} people you have not written to yet"]
    if st.counts.needs_you:
        parts.append(f"and {st.counts.needs_you} {'draft' if st.counts.needs_you == 1 else 'drafts'} "
                     f"waiting for you in Needs you")
    return " ".join(parts) + ". Nothing is sent until you approve it."


def default_suggestions(db: Any = None, user: Any = None, st: Optional[Status] = None) -> list[Suggestion]:
    """The bootstrap chips. With no candidate, one chip about the upload page.
    Called as (status), (db, user) or (db, user, status)."""
    if isinstance(db, Status):
        db, st = None, db
    no_resume = (st.state == "needs_resume") if st is not None else (
        db is not None and user is not None and resolve_agent_candidate(db, user_id_of(user)) is None)
    if no_resume:
        return [Suggestion(label="Upload your resume", prompt="Where do I upload my resume?")]
    return [
        Suggestion(label="Write to my top 5", prompt="Write emails to my top 5 matches."),
        Suggestion(label="Why these people?", prompt="Why are my top matches a good fit for me?"),
        Suggestion(label="Where are my emails?", prompt="Where do my emails stand?"),
    ]
