"""Drafts (the d_<id> outreach). Part of services/agent/store.py.

Import these through `services.agent.store`, never from this module directly.
The store keeps the row consistent (versions, approval record, the one live
row per lead); which transition is allowed for a student action is decided
in services/agent/actions.py, which calls these.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import AgentDraft, Candidate, Lead
from services.agent.store import (
    DRAFT_FAILED_REASON, DRAFT_STATUSES, LIVE_DRAFT_STATUSES, STALE_DRAFT_SECONDS, WAITING_DRAFT_STATUSES,
    NotFound, VersionConflict, WrongState, _now, _owned, iso,
)


def approval_hash(subject: str, body: str) -> str:
    """SHA-256 of subject + blank line + body, stored on approval (contract section 3, route 11)."""
    return hashlib.sha256(f"{subject or ''}\n\n{body or ''}".encode("utf-8")).hexdigest()


def _live_row(db: Session, candidate_id: int, lead_id: int, kind: str) -> Optional[AgentDraft]:
    return (db.query(AgentDraft).filter(AgentDraft.candidate_id == candidate_id, AgentDraft.lead_id == lead_id,
                                        AgentDraft.kind == kind, AgentDraft.status.in_(LIVE_DRAFT_STATUSES))
            .first())


def insert_drafting(db: Session, user_id: str, candidate_id: int, lead_id: int, *, thread_id: Optional[int] = None,
                    run_id: Optional[int] = None, style: Optional[str] = None, instructions: Optional[str] = None,
                    kind: str = "first_touch", status: str = "drafting", skip_reason: Optional[str] = None
                    ) -> AgentDraft:
    """A new `drafting` row (or a `skipped` one for a bare match) for a lead of the user's candidate.
    NotFound when the candidate or lead is not theirs. A live row (drafting, needs_review, approved,
    skipped) raises WrongState(row) with reason `skipped` or `wrong_state`. A `failed` row for the
    lead is superseded first. Flushes."""
    if status not in ("drafting", "skipped"):
        raise ValueError(f"insert_drafting status {status!r}")
    cand = db.get(Candidate, candidate_id)
    lead = db.get(Lead, lead_id)
    if cand is None or cand.user_id != user_id or lead is None or lead.candidate_id != candidate_id:
        raise NotFound()
    live = _live_row(db, candidate_id, lead_id, kind)
    if live is not None:
        raise WrongState(live, "skipped" if live.status == "skipped" else "wrong_state")
    now = _now()
    db.execute(update(AgentDraft).where(AgentDraft.candidate_id == candidate_id, AgentDraft.lead_id == lead_id,
                                        AgentDraft.kind == kind, AgentDraft.status == "failed")
               .values(status="superseded", updated_at=now))
    row = AgentDraft(user_id=user_id, candidate_id=candidate_id, lead_id=lead_id, thread_id=thread_id, run_id=run_id,
                     kind=kind, status=status, style=style, instructions=(instructions or None) and instructions[:400],
                     subject="", body="", version=0, versions=[], edited_by="agent", facts_used=[], flags=[],
                     heartbeat_at=now if status == "drafting" else None,
                     skipped_at=now if status == "skipped" else None,
                     skip_reason=(skip_reason or None) and skip_reason[:200], created_at=now, updated_at=now)
    try:
        with db.begin_nested():
            db.add(row)
    except IntegrityError:
        live = _live_row(db, candidate_id, lead_id, kind)
        raise WrongState(live, "skipped" if live is not None and live.status == "skipped" else "wrong_state") \
            from None
    return row


def insert_skipped(db: Session, user_id: str, candidate_id: int, lead_id: int, *, reason: Optional[str] = None,
                   thread_id: Optional[int] = None, run_id: Optional[int] = None) -> AgentDraft:
    """Skip a bare match: a `skipped` row with empty copy."""
    return insert_drafting(db, user_id, candidate_id, lead_id, thread_id=thread_id, run_id=run_id,
                           status="skipped", skip_reason=reason)


def _reap_rows(db: Session, rows: Iterable[AgentDraft], now: Optional[datetime] = None) -> int:
    cutoff = (now or _now()) - timedelta(seconds=STALE_DRAFT_SECONDS)
    n = 0
    for d in rows:
        if d.status == "drafting" and (d.heartbeat_at or d.created_at) < cutoff:
            d.status, d.failure_reason, d.heartbeat_at, d.updated_at = "failed", DRAFT_FAILED_REASON, None, _now()
            n += 1
    if n:
        db.commit()
    return n


def reap_stale_drafts(db: Session, user_id: Optional[str] = None, *, now: Optional[datetime] = None) -> int:
    """`drafting` rows silent for 5 minutes (the writer died) become `failed`. Commits when any did."""
    cutoff = (now or _now()) - timedelta(seconds=STALE_DRAFT_SECONDS)
    q = db.query(AgentDraft).filter(AgentDraft.status == "drafting",
                                    func.coalesce(AgentDraft.heartbeat_at, AgentDraft.created_at) < cutoff)
    if user_id is not None:
        q = q.filter(AgentDraft.user_id == user_id)
    return _reap_rows(db, q.all(), now)


def get_draft(db: Session, draft_id: int, user_id: str) -> AgentDraft:
    d = _owned(db.get(AgentDraft, draft_id), user_id)
    _reap_rows(db, [d])
    return d


def get_draft_internal(db: Session, draft_id: int) -> Optional[AgentDraft]:
    """Draft executor only (drafting.fill gets a bare id): the row whatever its owner, or None."""
    return db.get(AgentDraft, draft_id)


def list_drafts(db: Session, user_id: str, *, candidate_id: Optional[int] = None,
                statuses: Optional[Iterable[str]] = None, lead_ids: Optional[Iterable[int]] = None,
                ids: Optional[Iterable[int]] = None, limit: Optional[int] = None) -> list[AgentDraft]:
    """The user's drafts, newest `updated_at` first then id desc. Stale `drafting` rows are reaped first."""
    reap_stale_drafts(db, user_id)
    q = db.query(AgentDraft).filter(AgentDraft.user_id == user_id)
    if candidate_id is not None:
        q = q.filter(AgentDraft.candidate_id == candidate_id)
    if statuses is not None:
        q = q.filter(AgentDraft.status.in_(list(statuses)))
    if lead_ids is not None:
        q = q.filter(AgentDraft.lead_id.in_(list(lead_ids)))
    if ids is not None:
        q = q.filter(AgentDraft.id.in_(list(ids)))
    q = q.order_by(AgentDraft.updated_at.desc(), AgentDraft.id.desc())
    return q.limit(limit).all() if limit else q.all()


def live_rows_by_lead(db: Session, user_id: str, candidate_id: int, lead_ids: Optional[Iterable[int]] = None,
                      *, kind: str = "first_touch") -> dict[int, AgentDraft]:
    """lead_id -> its live row (drafting, needs_review, approved or skipped)."""
    rows = list_drafts(db, user_id, candidate_id=candidate_id, statuses=LIVE_DRAFT_STATUSES, lead_ids=lead_ids)
    return {d.lead_id: d for d in rows if d.kind == kind}


def waiting_count(db: Session, user_id: str) -> int:
    """drafting plus needs_review: what AGENT_MAX_WAITING_DRAFTS caps."""
    reap_stale_drafts(db, user_id)
    return (db.query(func.count(AgentDraft.id))
            .filter(AgentDraft.user_id == user_id, AgentDraft.status.in_(WAITING_DRAFT_STATUSES)).scalar() or 0)


def heartbeat_draft(db: Session, draft_id: int, *, commit: bool = True) -> None:
    db.execute(update(AgentDraft).where(AgentDraft.id == draft_id, AgentDraft.status == "drafting")
               .values(heartbeat_at=_now()))
    db.commit() if commit else db.flush()


def _check_version(d: AgentDraft, expected_version: Optional[int]) -> None:
    if expected_version is not None and expected_version != d.version:
        raise VersionConflict(d)


def _clear_approval(d: AgentDraft) -> None:
    d.approved_at = d.approved_version = d.approved_hash = None


def update_draft_copy(db: Session, draft_id: int, user_id: str, *, subject: str, body: str,
                      edited_by: str = "agent", expected_version: Optional[int] = None,
                      facts_used: Optional[list] = None, flags: Optional[list] = None,
                      style: Optional[str] = None, instructions: Optional[str] = None) -> AgentDraft:
    """New copy for a draft. From `drafting` (the writer's first fill): version 1, originals kept,
    status needs_review. From `needs_review` or `approved` (revise or edit): the old copy is pushed
    onto `versions` (newest first), version + 1, approval cleared, status needs_review. A stale
    expected_version raises VersionConflict; other states raise WrongState. Flushes."""
    if edited_by not in ("agent", "student"):
        raise ValueError("edited_by")
    d = _owned(db.get(AgentDraft, draft_id), user_id)
    now = _now()
    if d.status == "drafting":
        d.version, d.original_subject, d.original_body, d.heartbeat_at = 1, subject, body, None
    elif d.status in ("needs_review", "approved"):
        _check_version(d, expected_version)
        old = {"version": d.version, "subject": d.subject, "body": d.body, "edited_by": d.edited_by,
               "created_at": iso(d.updated_at)}
        d.versions = [old] + list(d.versions or [])
        d.version = d.version + 1
        _clear_approval(d)
    else:
        raise WrongState(d)
    d.subject, d.body, d.edited_by, d.status, d.updated_at = subject, body, edited_by, "needs_review", now
    if facts_used is not None:
        d.facts_used = [dict(f) for f in facts_used]
    if flags is not None:
        d.flags = [dict(f) for f in flags]
    if style is not None:
        d.style = style
    if instructions is not None:
        d.instructions = instructions[:400] or None
    db.flush()
    return d


def set_draft_status(db: Session, draft_id: int, user_id: str, status: str, *,
                     expected_version: Optional[int] = None, reason: Optional[str] = None) -> AgentDraft:
    """Move a draft and keep its record consistent: approved stores approved_at, approved_version and
    approval_hash; skipped stores skipped_at and skip_reason; discarded stores discarded_at and
    discard_reason; failed stores failure_reason; needs_review (a restore) clears approval and skip.
    Transition rules live in actions.py. Flushes."""
    if status not in DRAFT_STATUSES:
        raise ValueError(f"draft status {status!r}")
    d = _owned(db.get(AgentDraft, draft_id), user_id)
    _check_version(d, expected_version)
    now = _now()
    why = (reason or None) and reason[:200]
    if status == "approved":
        d.approved_at, d.approved_version, d.approved_hash = now, d.version, approval_hash(d.subject, d.body)
    elif status == "skipped":
        d.skipped_at, d.skip_reason = now, why
    elif status == "discarded":
        d.discarded_at, d.discard_reason = now, why
    elif status == "failed":
        d.failure_reason, d.heartbeat_at = why or "failed", None
    elif status == "needs_review":
        d.skipped_at = d.skip_reason = None
    if status in ("needs_review", "skipped", "discarded", "superseded"):
        _clear_approval(d)
    d.status, d.updated_at = status, now
    db.flush()
    return d


def delete_draft(db: Session, draft_id: int, user_id: str) -> None:
    """Remove a row outright (a restored skip that never had copy)."""
    d = _owned(db.get(AgentDraft, draft_id), user_id)
    db.delete(d)
    db.flush()
