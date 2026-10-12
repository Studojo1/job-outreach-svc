"""Workspace mutations: one function per action, two callers.

Every workspace route and every agent tool that changes something calls the
same function here (contract invariant 4), so chat and tabs cannot disagree.
Functions flush through `store`; the caller commits. Refusals raise
`ActionError`; `store.NotFound`, `store.WrongState` and
`store.VersionConflict` may also surface and are mapped by the route.

Phase 1 never sends: nothing here writes emails_sent, campaigns, orders or
credits. The only write outside the agent tables is `update_notes`
(candidates.flex_notes).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, Candidate, EmailSent, Lead
from services.agent import reads, store
from services.agent import status_words as sw
from services.agent.context import (
    ActionError,
    not_found,
    owned_lead,
    parse_outreach_id,
    resolve_agent_candidate,
    user_id_of,
)
from services.agent.events import strip_dashes
from services.agent.schemas import (
    AgentSettings,
    ApproveAllResult,
    ApproveResult,
    DraftsCreated,
    Memory,
    OutreachDetail,
    OutreachRow,
    PreviousCopy,
    Profile,
    Refusal,
    ReviseResult,
    UpdateNotes,
    UpdateSettings,
)
from services.agent.schemas_chat import OkResult

__all__ = ["ActionError", "start_drafts", "submit_drafts", "revise", "edit", "approve", "approve_all", "discard",
           "skip_lead", "restore", "update_notes", "save_memory", "delete_memory", "update_settings"]

logger = get_logger(__name__)

APPROVED_MESSAGE = "Approved. Sending comes next, so nothing has gone out."
TOKENS_PER_WRITE = 1900   # generate_email_for_lead reports no usage; a flat estimate per draft or revision
SENT_IS_FINAL = "That email has already gone out, so it can't be changed."


# ---------------------------------------------------------------- helpers

def _draft_id(outreach_id: Any) -> int:
    parsed = parse_outreach_id(outreach_id)
    if parsed is None:
        raise not_found()
    kind, num = parsed
    if kind == "e":
        raise ActionError("wrong_state", SENT_IS_FINAL, 409)
    return num


def _draft(db: Session, uid: str, outreach_id: Any):
    try:
        return store.get_draft(db, _draft_id(outreach_id), uid)
    except store.NotFound:
        raise not_found() from None


def _lead(db: Session, lead_id: int) -> Optional[Lead]:
    return db.get(Lead, lead_id)


def _detail(db: Session, d) -> OutreachDetail:
    return sw.draft_detail(d, _lead(db, d.lead_id))


def _has_touch1(db: Session, uid: str, lead_id: int) -> bool:
    return db.query(EmailSent.id).join(Campaign, Campaign.id == EmailSent.campaign_id) \
        .join(Candidate, Candidate.id == Campaign.candidate_id) \
        .filter(Candidate.user_id == uid, EmailSent.lead_id == lead_id, EmailSent.followup_number == 0,
                EmailSent.is_test.isnot(True), EmailSent.status.in_(tuple(sw.EMAIL_WORDS))).first() is not None


def _clean(text: Optional[str]) -> Optional[str]:
    return strip_dashes(text.strip()) if isinstance(text, str) else text


# ---------------------------------------------------------------- drafts

def start_drafts(db: Session, user: Any, lead_ids: Iterable[int], *, style: Optional[str] = None,
                 instructions: Optional[str] = None, thread_id: Optional[int] = None,
                 run_id: Optional[int] = None, max_new: Optional[int] = None) -> DraftsCreated:
    """Route 8 and draft_emails. Per lead, in order: ownership (not_found),
    never-contact, skipped, live row or already emailed (wrong_state), then the
    waiting and daily caps (draft_limit). Accepted leads get `drafting` rows
    now; the caller commits, then calls `submit_drafts`. Refusals never raise."""
    uid = user_id_of(user)
    rules = reads.settings(db, uid).never_contact
    waiting = store.waiting_count(db, uid)
    used_today = store.usage_today(db, uid).drafts
    wait_cap = reads.cap("AGENT_MAX_WAITING_DRAFTS", 10)
    day_cap = reads.cap("AGENT_DAILY_DRAFT_CAP", 30)
    instructions = _clean(instructions) or None
    items: list[OutreachRow] = []
    refused: list[Refusal] = []

    def refuse(lid: int, code: str, message: str) -> None:
        refused.append(Refusal(lead_id=lid, code=code, message=message))

    seen: set[int] = set()
    for raw in lead_ids:
        lid = int(raw)
        if lid in seen:
            continue
        seen.add(lid)
        lead = owned_lead(db, uid, lid)
        if lead is None:
            refuse(lid, "not_found", "I couldn't find that person in your matches.")
            continue
        if reads.never_contact_hit(rules, lead.name, lead.company):
            refuse(lid, "never_contact", f"{lead.name or 'This person'} is on your never contact list.")
            continue
        live = store.live_rows_by_lead(db, uid, lead.candidate_id, [lid]).get(lid)
        if live is not None and live.status == "skipped":
            refuse(lid, "skipped", f"You passed on {lead.name or 'this person'}. Restore them first.")
            continue
        if live is not None:
            refuse(lid, "wrong_state", f"There is already a draft for {lead.name or 'this person'}.")
            continue
        if _has_touch1(db, uid, lid):
            refuse(lid, "wrong_state", f"You already emailed {lead.name or 'this person'}.")
            continue
        if max_new is not None and len(items) >= max_new:
            refuse(lid, "draft_limit", "I write up to 5 emails per message. Ask me again for the next ones.")
            continue
        if waiting + len(items) >= wait_cap:
            refuse(lid, "draft_limit", f"You have {wait_cap} drafts waiting. Review some before I write more.")
            continue
        if used_today + len(items) >= day_cap:
            refuse(lid, "draft_limit", f"You reached today's limit of {day_cap} drafts. It resets at midnight IST.")
            continue
        try:
            row = store.insert_drafting(db, uid, lead.candidate_id, lid, thread_id=thread_id, run_id=run_id,
                                        style=style, instructions=instructions)
        except store.WrongState as e:
            code = "skipped" if e.reason == "skipped" else "wrong_state"
            refuse(lid, code, "That person changed just now. Try again.")
            continue
        except store.NotFound:
            refuse(lid, "not_found", "I couldn't find that person in your matches.")
            continue
        items.append(sw.draft_row(row, lead))
    if items:
        store.bump_usage(db, uid, drafts=len(items), tokens=TOKENS_PER_WRITE * len(items))
        if run_id is not None:
            try:
                store.add_usage(db, run_id, drafts_created=len(items), commit=False)
            except store.NotFound:
                pass
    return DraftsCreated(items=items, refused=refused)


def submit_drafts(result: DraftsCreated) -> list[int]:
    """After the caller committed start_drafts: hand the rows to this pod's writers."""
    from services.agent import drafting

    ids = [int(r.id[2:]) for r in result.items]
    drafting.submit(ids)
    return ids


def revise(db: Session, user: Any, outreach_id: Any, instruction: str, *,
           expected_version: Optional[int] = None) -> ReviseResult:
    """Route 9 and revise_draft. Synchronous: one writer call. The session is
    committed (so no connection is held) before the model call."""
    from services.agent import drafting

    uid = user_id_of(user)
    d = _draft(db, uid, outreach_id)
    if d.status not in sw.EDITABLE:
        raise ActionError("wrong_state", "Only a draft waiting for you or an approved one can be rewritten.", 409)
    if expected_version is not None and expected_version != d.version:
        raise store.VersionConflict(d)
    revision_cap = reads.cap("AGENT_DAILY_REVISION_CAP", 60)
    if store.usage_today(db, uid).revisions >= revision_cap:
        raise ActionError("draft_limit", f"You reached today's limit of {revision_cap} rewrites. "
                          "It resets at midnight IST.", 429, limit="revisions")
    previous = PreviousCopy(subject=d.subject, body=d.body, version=d.version)
    draft_id, version, lead_id, cand_id = d.id, d.version, d.lead_id, d.candidate_id
    inputs = drafting.load_inputs(db, uid, lead_id, cand_id)
    db.commit()
    try:
        subject, body, facts, flags = drafting.revise_copy(previous.subject, previous.body,
                                                          _clean(instruction) or "", inputs)
    except drafting.ContentFilterError:
        raise ActionError("invalid", "I can't make that change. Try asking another way.", 422) from None
    except Exception:
        logger.exception("[AGENT] revise failed for draft %s", draft_id)
        raise ActionError("busy", "I couldn't rewrite it just now. Please try again.", 503) from None
    d = store.update_draft_copy(db, draft_id, uid, subject=subject, body=body, edited_by="agent",
                                expected_version=version, facts_used=facts, flags=flags)
    store.bump_usage(db, uid, revisions=1, tokens=TOKENS_PER_WRITE)
    return ReviseResult(outreach=_detail(db, d), previous=previous)


def edit(db: Session, user: Any, outreach_id: Any, *, subject: Optional[str] = None, body: Optional[str] = None,
         expected_version: Optional[int] = None) -> OutreachDetail:
    """Route 10: the student's own words, verbatim apart from dash stripping."""
    uid = user_id_of(user)
    d = _draft(db, uid, outreach_id)
    if subject is None and body is None:
        raise ActionError("invalid", "Send a new subject or body.", 422)
    if d.status not in sw.EDITABLE:
        raise ActionError("wrong_state", "This draft can't be edited right now.", 409)
    d = store.update_draft_copy(db, d.id, uid, subject=strip_dashes(subject) if subject is not None else d.subject,
                                body=strip_dashes(body) if body is not None else d.body, edited_by="student",
                                expected_version=expected_version, flags=[])
    return _detail(db, d)


def approve(db: Session, user: Any, outreach_id: Any, *, expected_version: Optional[int] = None) -> ApproveResult:
    """Route 11. Records the approval of this exact version. Never sends."""
    uid = user_id_of(user)
    d = _draft(db, uid, outreach_id)
    if expected_version is not None and expected_version != d.version:
        raise store.VersionConflict(d)
    if d.status == "approved" and d.approved_version == d.version:
        return ApproveResult(outreach=_detail(db, d), message=APPROVED_MESSAGE)  # a retried click
    if d.status != "needs_review":
        raise ActionError("wrong_state", "Only a draft waiting for you can be approved.", 409)
    d = store.set_draft_status(db, d.id, uid, "approved", expected_version=expected_version)
    return ApproveResult(outreach=_detail(db, d), message=APPROVED_MESSAGE)


def approve_all(db: Session, user: Any, ids: Iterable[str]) -> ApproveAllResult:
    """Route 12: approve the current version of each needs_review id; refuse the rest."""
    uid = user_id_of(user)
    approved: list[OutreachRow] = []
    refused: list[Refusal] = []
    for oid in ids:
        try:
            d = _draft(db, uid, oid)
        except ActionError as e:
            refused.append(Refusal(outreach_id=str(oid), code=e.code, message=e.message))
            continue
        if d.status != "needs_review":
            refused.append(Refusal(outreach_id=str(oid), code="wrong_state",
                                   message="Only drafts waiting for you can be approved."))
            continue
        d = store.set_draft_status(db, d.id, uid, "approved")
        approved.append(sw.draft_row(d, _lead(db, d.lead_id)))
    n = len(approved)
    message = (f"Approved {n} {'draft' if n == 1 else 'drafts'}. Sending comes next, so nothing has gone out."
               if n else "Nothing was approved.")
    return ApproveAllResult(approved=approved, refused=refused, message=message)


def discard(db: Session, user: Any, outreach_id: Any, *, reason: Optional[str] = None) -> OutreachDetail:
    """Route 13: hide the draft; the lead is a match again."""
    uid = user_id_of(user)
    d = _draft(db, uid, outreach_id)
    if d.status not in sw.DISCARDABLE:
        raise ActionError("wrong_state", "This one can't be discarded.", 409)
    d = store.set_draft_status(db, d.id, uid, "discarded", reason=_clean(reason))
    return _detail(db, d)


def skip_lead(db: Session, user: Any, lead_id: Any, *, reason: Optional[str] = None,
              thread_id: Optional[int] = None, run_id: Optional[int] = None) -> OutreachRow:
    """Route 5 and skip. A bare match gets a skipped row with empty copy; a live
    draft moves to skipped. Already skipped returns the row (a retried click)."""
    uid = user_id_of(user)
    lead = owned_lead(db, uid, lead_id)
    if lead is None:
        raise not_found("I couldn't find that person in your matches.")
    live = store.live_rows_by_lead(db, uid, lead.candidate_id, [lead.id]).get(lead.id)
    if live is not None and live.status == "skipped":
        return sw.draft_row(live, lead)
    if live is not None:
        d = store.set_draft_status(db, live.id, uid, "skipped", reason=_clean(reason))
    else:
        if _has_touch1(db, uid, lead.id):
            raise ActionError("wrong_state", f"You already emailed {lead.name or 'this person'}.", 409)
        d = store.insert_skipped(db, uid, lead.candidate_id, lead.id, reason=_clean(reason),
                                 thread_id=thread_id, run_id=run_id)
    return sw.draft_row(d, lead)


def restore(db: Session, user: Any, outreach_id: Any) -> OkResult:
    """Route 14: undo a skip. Back to needs_review when it has copy, else deleted."""
    uid = user_id_of(user)
    d = _draft(db, uid, outreach_id)
    if d.status != "skipped":
        raise ActionError("wrong_state", "Only someone you passed on can be restored.", 409)
    if (d.body or "").strip():
        store.set_draft_status(db, d.id, uid, "needs_review")
    else:
        store.delete_draft(db, d.id, uid)
    return OkResult()


# ---------------------------------------------------------------- profile notes, memories, settings

def update_notes(db: Session, user: Any, body: Any) -> Profile:
    """Route 17 and the confirmed update_profile_notes ticket. Merges key by
    key into candidates.flex_notes; "" removes a key; None leaves it."""
    uid = user_id_of(user)
    notes = body if isinstance(body, UpdateNotes) else UpdateNotes(**dict(body))
    cand = resolve_agent_candidate(db, uid)
    if cand is None:
        raise not_found("Upload your resume first, then I can save notes to your profile.")
    flex = dict(cand.flex_notes) if isinstance(cand.flex_notes, dict) else {}
    for key in reads.NOTE_KEYS:
        value = getattr(notes, key)
        if value is None:
            continue
        value = _clean(value) or ""
        if value:
            flex[key] = value
        else:
            flex.pop(key, None)
    cand.flex_notes = flex
    db.flush()
    return reads.profile(db, uid, candidate=cand)


def save_memory(db: Session, user: Any, text: str, *, scope: str = "everywhere", company: Optional[str] = None,
                use_in_emails: bool = False, source: str = "chat") -> Memory:
    try:
        m = store.save_memory(db, user_id_of(user), _clean(text) or "", scope=scope, company=_clean(company),
                              use_in_emails=use_in_emails, source=source)
    except store.WrongState:
        raise ActionError("wrong_state", f"You have {store.MEMORY_CAP} saved notes. Forget one first.", 409) from None
    except ValueError:
        raise ActionError("invalid", "That note is empty.", 422) from None
    return store.memory_out(m)


def delete_memory(db: Session, user: Any, memory_id: int) -> Memory:
    try:
        return store.delete_memory(db, int(memory_id), user_id_of(user))
    except store.NotFound:
        raise not_found("I couldn't find that saved note.") from None


def update_settings(db: Session, user: Any, body: Any) -> AgentSettings:
    s = body if isinstance(body, UpdateSettings) else UpdateSettings(**dict(body))
    return store.update_settings(db, user_id_of(user), review_before_send=s.review_before_send,
                                 followups=s.followups, never_contact=s.never_contact)
