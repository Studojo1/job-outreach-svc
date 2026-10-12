"""Confirmation tickets: the student's click, executed (contract section 7).

Phase 1 has one ticketed tool, `update_profile_notes`. The tool itself only
creates the ticket (tools.py). Confirm or Cancel then arrives as a normal turn
whose `SendMessage.confirmation` names the ticket:

- `validate` runs in the route before any byte is streamed: the ticket must be
  the user's, on this thread, pending and unexpired, else 409 wrong_state
  "That request expired. Ask me again."
- `execute` runs inside the turn: approve runs the STORED args (never new
  ones from the model) through `actions.update_notes` inside a
  tool_start/tool_end pair that invalidates `profile`; decline just records
  it. Either way the updated ticket is emitted, and the returned system line
  asks the model for a one-line acknowledgement.
"""

from __future__ import annotations

from typing import Any

from core.logger import get_logger
from services.agent import context, prompt, store
from services.agent.gating import agent_error

logger = get_logger(__name__)

EXPIRED = "That request expired. Ask me again."
LABELS = {"update_profile_notes": "Saving your profile notes"}


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def validate(db: Any, user_id: str, thread_id: int, decision: Any) -> None:
    """Raise 409 wrong_state unless the ticket can be decided on this thread now."""
    try:
        store.get_confirmation(db, _field(decision, "id"), user_id, thread_id=thread_id, require_pending=True)
    except (store.NotFound, store.WrongState):
        db.commit()  # keeps a pending-past-expiry ticket marked expired
        raise agent_error("wrong_state", EXPIRED) from None


def _run_action(ctx: Any, tool: str, args: dict) -> None:
    """Execute a ticket's stored args. Only the tools listed here can ever run."""
    from services.agent import actions
    from services.agent.schemas import UpdateNotes

    if tool != "update_profile_notes":
        raise ValueError("no such confirmable tool")
    db = context.open_session()
    try:
        actions.update_notes(db, ctx.user_id, UpdateNotes(**args))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _claim(ctx: Any, cid: str) -> bool:
    db = context.open_session()
    try:
        claimed = store.claim_confirmation(db, cid, ctx.user_id, thread_id=ctx.thread_id)
        db.commit()
        return claimed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _record(ctx: Any, cid: str, ok: bool) -> Any:
    db = context.open_session()
    try:
        out = store.confirmation_out(store.record_confirmation_result(db, cid, ctx.user_id, ok=ok))
        db.commit()
        return out
    except Exception as e:
        db.rollback()
        logger.warning("[AGENT] could not record ticket %s: %s", cid, type(e).__name__)
        return None
    finally:
        db.close()


def _decide(ctx: Any, cid: str, status: str, result: dict) -> Any:
    db = context.open_session()
    try:
        row = store.decide_confirmation(db, cid, ctx.user_id, status, result=result)
        out = store.confirmation_out(row)
        db.commit()
        return out
    except store.WrongState:
        db.rollback()
        return None
    finally:
        db.close()


def _current(ctx: Any, cid: str) -> tuple[str, dict, str, Any]:
    """(tool, stored args, summary, Confirmation) read in one short session."""
    db = context.open_session()
    try:
        row = store.get_confirmation(db, cid, ctx.user_id, thread_id=ctx.thread_id)
        found = (row.tool, dict(row.args or {}), row.summary, store.confirmation_out(row))
        db.commit()
        return found
    finally:
        db.close()


def execute(ctx: Any, decision: Any, emit: Any) -> str:
    """Carry out (or decline) the ticket and return the acknowledgement line."""
    cid = str(_field(decision, "id"))
    approve = _field(decision, "decision") == "approve"
    tool, args, summary, out = _current(ctx, cid)
    stale = prompt.CONFIRM_ACK_LINE.format(
        decision="Confirm" if approve else "Cancel", summary=summary,
        outcome="It had already expired or been decided, so nothing changed. Offer to prepare it again.")
    if out.status != "pending":
        emit.emit({"type": "confirmation", "confirmation": out.model_dump(mode="json")})
        return stale

    if not approve:
        decided = _decide(ctx, cid, "declined", {"ok": True})
        emit.emit({"type": "confirmation", "confirmation": (decided or out).model_dump(mode="json")})
        return prompt.CONFIRM_ACK_LINE.format(decision="Cancel", summary=summary,
                                              outcome="Nothing was changed.")

    # Claim first: only the caller that moves the ticket from pending runs it,
    # so two decisions that race (two tabs, two pods) can never both act.
    if not _claim(ctx, cid):
        _, _, _, now_out = _current(ctx, cid)
        emit.emit({"type": "confirmation", "confirmation": now_out.model_dump(mode="json")})
        return stale
    call_id = f"confirm_{cid}"
    emit.emit({"type": "tool_start", "call_id": call_id, "tool": tool,
               "label": LABELS.get(tool, "Saving that")})
    ok = True
    try:
        _run_action(ctx, tool, args)
    except Exception as e:
        logger.warning("[AGENT] confirmed action %s failed in run %s: %s", tool, ctx.run_id, type(e).__name__)
        ok = False
    decided = _record(ctx, cid, ok)
    emit.emit({"type": "tool_end", "call_id": call_id, "tool": tool, "ok": ok,
               "summary": "Profile notes saved" if ok else "Couldn't save that",
               "invalidate": ["profile"] if ok else []})
    emit.emit({"type": "confirmation", "confirmation": (decided or out).model_dump(mode="json")})
    outcome = ("The change is saved, and every new email will use it." if ok
               else "Saving failed, so nothing changed. Suggest trying again.")
    return prompt.CONFIRM_ACK_LINE.format(decision="Confirm", summary=summary, outcome=outcome)
