"""The student agent's turn loop (design 4.4, contract sections 4 and 7).

`run_turn(run_id, q, slot)` is the worker thread's entry point and never
raises. It loads the run's context, emits `run_start`, then alternates model
calls (streamed text goes straight to the queue) and tool calls (through
`tools.dispatch`, so tool rows and their frames come from one place) until
the model answers with text only, the step or wall-clock budget runs out, or
the student presses Stop. It ends with `suggestions` (optional),
`message_end` and `done`, or with exactly one `error` and no assistant message.

No pooled connection is held across a model call: every store call opens a
short session through `context.open_session` and closes it. A ticker thread
bumps `heartbeat_at` every 10 s while the run is alive, so a draft wait of 90 s
never looks like a dead pod to the stale-run reaper.

In tests, call `run_turn` on the test thread with a plain `queue.Queue` and
read the frames back; `llm.stream_chat` is the only seam to fake.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Optional

from core.config import settings
from core.logger import get_logger
from services.agent import context, events, llm, prompt, store, tools
from services.agent.events import Emitter, strip_dashes
from database.models import AgentRun
from services.agent.gating import NullSlot

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 10
ERROR_COPY = {
    "internal": ("Something went wrong on our side. Please try again.", True),
    "model_unavailable": ("I couldn't reach my model just now. Please try again.", True),
    "content_filter": ("I can't help with that one. Try asking another way.", False),
}
TOOL_FAILED = "TOOL ERROR: that lookup failed. Tell the student plainly or try another way."
BUDGET_REFUSED = ("REFUSED: tool budget for this turn is used up. Tell the student what is done "
                  "and what is left.")

# Follow-up chips chosen in code from the tools that ran (contract section 4).
_SUGGESTIONS: dict[str, list[tuple[str, str]]] = {
    "draft_emails": [("Make it shorter", "Make the last draft shorter."),
                     ("Write to 5 more", "Write emails to my next 5 matches."),
                     ("Where are my emails?", "Where do my emails stand?")],
    "revise_draft": [("Make it warmer", "Make the last draft a little warmer."),
                     ("Shorter subject", "Give the last draft a shorter subject line."),
                     ("Show what needs me", "Show the drafts that need me.")],
    "list_matches": [("Write to my top 5", "Write emails to my top 5 matches."),
                     ("Only strong matches", "Show only my strong matches.")],
    "get_match": [("Write to this person", "Write an email to this person."),
                  ("Pass on them", "Pass on this person.")],
    "list_outreach": [("Show what needs me", "Show the drafts that need me."),
                      ("Write to my top 5", "Write emails to my top 5 matches.")],
    "get_outreach": [("Make it shorter", "Make this email shorter."),
                     ("Make it warmer", "Make this email a little warmer.")],
    "get_profile": [("Write to my top 5", "Write emails to my top 5 matches."),
                    ("Why these people?", "Why are my top matches a good fit for me?")],
    "get_status": [("Write to my top 5", "Write emails to my top 5 matches."),
                   ("Why these people?", "Why are my top matches a good fit for me?")],
    "skip": [("Who's next?", "Who is my next best match?")],
}


@contextmanager
def _db():
    db = context.open_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _cancel_requested(run_id: int) -> bool:
    try:
        with _db() as db:
            return store.cancel_requested(db, run_id)
    except Exception:
        logger.warning("[AGENT] cancel check failed for run %s", run_id)
        return False


class _Heartbeat:
    """Bumps agent_runs.heartbeat_at every HEARTBEAT_SECONDS until stopped."""

    def __init__(self, run_id: int):
        self.run_id = run_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"agent-hb-{run_id}", daemon=True)

    def start(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                with _db() as db:
                    store.heartbeat(db, self.run_id)
            except Exception:
                logger.warning("[AGENT] heartbeat failed for run %s", self.run_id)

    def stop(self) -> None:
        self._stop.set()


def _record_usage(run_id: int, turn: Any, steps: int = 1) -> None:
    usage = getattr(turn, "usage", None)
    with _db() as db:
        store.add_usage(db, run_id, usage, steps=steps)


def _call_id(call: Any) -> str:
    return str(getattr(call, "id", None) or (call.get("id") if isinstance(call, dict) else "") or "")


def _call_name(call: Any) -> str:
    return str(getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else "") or "")


def _openai_tool_calls(turn: Any) -> list[dict]:
    if hasattr(turn, "as_message"):
        return turn.as_message().get("tool_calls") or []
    return [{"id": _call_id(c), "type": "function",
             "function": {"name": _call_name(c), "arguments": getattr(c, "arguments", "{}") or "{}"}}
            for c in turn.tool_calls]


def _dispatch(ctx: Any, call: Any, emit: Emitter) -> tuple[str, bool, str]:
    """Run one tool through tools.dispatch. Returns (for_model, ok, summary).
    A raising tool becomes TOOL ERROR for the model and ok false for the UI,
    and a step row left running gets its tool_end."""
    before = len(emit.frames)
    try:
        res = tools.dispatch(ctx, call, emit)
        return (str(getattr(res, "for_model", "") or ""), bool(getattr(res, "ok", True)),
                str(getattr(res, "summary", "") or ""))
    except Exception as e:
        # Type only: a validation error's text quotes its input (draft copy, names).
        logger.warning("[AGENT] tool %s raised in run %s: %s", _call_name(call), ctx.run_id, type(e).__name__)
        logger.debug("[AGENT] tool traceback", exc_info=True)
        cid = _call_id(call)
        new = emit.frames[before:]
        started = any(f["type"] == "tool_start" and f["call_id"] == cid for f in new)
        ended = any(f["type"] == "tool_end" and f["call_id"] == cid for f in new)
        if started and not ended:
            emit.emit({"type": "tool_end", "call_id": cid, "tool": _call_name(call), "ok": False,
                       "summary": "That didn't work", "invalidate": []})
        return TOOL_FAILED, False, "That didn't work"


def build_messages(ctx: Any) -> list[dict]:
    """System prompt, the snapshot (data), then the thread history, which
    already ends with this run's user message (start_run committed it)."""
    try:
        snapshot = context.snapshot(ctx)
    except Exception:
        logger.warning("[AGENT] snapshot failed for run %s; using a minimal one", ctx.run_id, exc_info=True)
        snapshot = prompt.build_snapshot(today=time.strftime("%Y-%m-%d", time.gmtime()),
                                         first_name=ctx.first_name)
    with _db() as db:
        history = store.history_for_model(db, ctx.thread_id, ctx.user_id)
    return [{"role": "system", "content": prompt.SYSTEM_PROMPT},
            {"role": "system", "content": snapshot}, *history]


def _final_meta(emit: Emitter, status: str, suggestions: list[dict]) -> dict:
    steps: dict[str, dict] = {}
    for f in emit.of_type("tool_start", "tool_end"):
        if f["type"] == "tool_start":
            steps[f["call_id"]] = {"call_id": f["call_id"], "tool": f["tool"], "label": f["label"],
                                   "ok": None, "summary": None}
        elif f["call_id"] in steps:
            steps[f["call_id"]].update(ok=f["ok"], summary=f["summary"])
    outreach_ids: list[str] = []
    for f in emit.of_type("draft"):
        oid = f["outreach"]["id"]
        if oid not in outreach_ids:
            outreach_ids.append(oid)
    conf_ids: list[str] = []
    for f in emit.of_type("confirmation"):
        cid = f["confirmation"]["id"]
        if cid not in conf_ids:
            conf_ids.append(cid)
    navs = emit.of_type("navigate")
    nav = {"to": navs[-1]["to"], "id": navs[-1]["id"], "filter": navs[-1]["filter"]} if navs else None
    return {"steps": list(steps.values()), "cards": [f["card"] for f in emit.of_type("card")],
            "outreach_ids": outreach_ids, "confirmation_ids": conf_ids, "navigate": nav,
            "suggestions": suggestions, "status": status}


def pick_suggestions(tools_ran: list[str]) -> list[dict]:
    """At most three chips, from the last tool that has any; none otherwise."""
    for name in reversed(tools_ran):
        if name in _SUGGESTIONS:
            return [{"label": label, "prompt": p} for label, p in _SUGGESTIONS[name][:3]]
    return []


def _usage_frame(user_id: str) -> dict:
    try:
        with _db() as db:
            used = store.usage_today(db, user_id)
        return {"messages_today": int(used.get("messages", 0)), "messages_cap": settings.AGENT_DAILY_MESSAGE_CAP}
    except Exception:
        return {"messages_today": 0, "messages_cap": settings.AGENT_DAILY_MESSAGE_CAP}


def _forward_terminal(emit: Emitter, user_id: Optional[str]) -> None:
    """The run was already made terminal elsewhere (the stale-run reaper wrote
    `error interrupted`). Put that persisted frame on the live queue, numbered
    after what this stream showed, so the stream still ends with exactly one
    terminal frame. Nothing is persisted again."""
    frame = None
    try:
        with _db() as db:
            frames = store.events_after(db, emit.run_id, user_id, 0)
        frame = next((f for f in reversed(frames) if f.get("type") in ("error", "done")), None)
    except Exception as e:
        logger.warning("[AGENT] could not read the terminal frame of run %s: %s", emit.run_id, type(e).__name__)
    if frame is None:
        frame = {"type": "error", "code": "interrupted", "message": store.INTERRUPTED_MESSAGE, "retryable": True}
    if emit.q is not None:
        try:
            emit.q.put(events.validate({**frame, "seq": emit.seq + 1}))
        except Exception as e:
            logger.warning("[AGENT] could not forward the terminal frame of run %s: %s", emit.run_id,
                           type(e).__name__)


def _is_running(run_id: int) -> bool:
    try:
        with _db() as db:
            run = db.get(AgentRun, run_id)
            return run is not None and run.status == "running"
    except Exception:
        return True  # unknown: let fail_run decide


def _tokens_used_up(user_id: str) -> bool:
    try:
        with _db() as db:
            used = store.usage_today(db, user_id)
        return int(used.get("tokens", 0)) >= settings.AGENT_DAILY_TOKEN_CAP
    except Exception:
        return False


def _finish(emit: Emitter, ctx: Any, status: str, segments: list[str], tools_ran: list[str]) -> None:
    content = strip_dashes("\n\n".join(s for s in segments if s)) or ""
    suggestions = pick_suggestions(tools_ran) if status != "cancelled" else []
    if suggestions:
        emit.emit({"type": "suggestions", "items": suggestions})
    meta = _final_meta(emit, status, suggestions)
    with _db() as db:
        msg = store.save_final_message(db, emit.run_id, content, meta=meta, user_id=ctx.user_id)
        message_id = int(msg.id)
        moved = store.finish_run(db, emit.run_id, status, user_id=ctx.user_id, commit=False)
        if not moved:
            # Reaped meanwhile (it looked dead). Keep its error; drop this reply.
            db.rollback()
            logger.warning("[AGENT] run %s was already terminal at finish", emit.run_id)
    if not moved:
        _forward_terminal(emit, ctx.user_id)
        return
    emit.emit({"type": "message_end", "message_id": message_id, "content": content})
    emit.emit({"type": "done", "run_id": emit.run_id, "status": status,
               "invalidate": emit.invalidate_union(), "usage": _usage_frame(ctx.user_id)})
    logger.info("[AGENT] run %s finished %s, %s tool calls", emit.run_id, status, len(tools_ran),
                extra={"event": "agent_run_end", "status": status})


def _fail(emit: Emitter, code: str, error: str, user_id: Optional[str] = None) -> None:
    if not _is_running(emit.run_id):
        # Already terminal (reaped as interrupted): its error frame stands; never add a second.
        _forward_terminal(emit, user_id)
        return
    message, retryable = ERROR_COPY.get(code, ERROR_COPY["internal"])
    frame = {"type": "error", "code": code if code in ERROR_COPY else "internal",
             "message": message, "retryable": retryable}
    try:
        emit.emit(frame)
    except Exception as e:
        logger.warning("[AGENT] could not persist the error frame of run %s: %s", emit.run_id, type(e).__name__)
        if emit.q is not None:
            emit.q.put(events.validate({**frame, "seq": emit.seq + 1}))
    try:
        with _db() as db:
            store.fail_run(db, emit.run_id, frame["code"], error)
    except Exception as e:
        logger.warning("[AGENT] could not mark run %s failed: %s", emit.run_id, type(e).__name__)


def _model_step(emit: Emitter, run_id: int, messages: list[dict], with_tools: bool) -> Any:
    turn = llm.stream_chat(messages, tools.TOOLS if with_tools else None,
                           on_text=emit.text, should_stop=lambda: _cancel_requested(run_id))
    _record_usage(run_id, turn)
    return turn


def _loop(emit: Emitter, ctx: Any, messages: list[dict]) -> tuple[str, list[str], list[str]]:
    """The tool-calling loop. Returns (status, text segments, tools that ran)."""
    run_id = emit.run_id
    deadline = time.monotonic() + settings.AGENT_RUN_WALL_SECONDS
    ctx.extra["deadline"] = deadline  # tools that wait (draft_emails) never wait past it
    segments: list[str] = []
    tools_ran: list[str] = []
    used = 0
    for step in range(settings.AGENT_MAX_STEPS):
        if _cancel_requested(run_id):
            return "cancelled", segments, tools_ran
        # Wall clock and today's token cap end the turn like the step limit: one
        # last call without tools, so the student still gets a summary.
        last = (step == settings.AGENT_MAX_STEPS - 1 or time.monotonic() > deadline
                or (step > 0 and _tokens_used_up(ctx.user_id)))
        msgs = messages + [{"role": "system", "content": prompt.LAST_STEP_LINE}] if last else messages
        turn = _model_step(emit, run_id, msgs, with_tools=not last)
        emit.take_text()
        text = (strip_dashes(turn.text) or "").strip()
        if text:
            segments.append(text)
        if getattr(turn, "stopped", False):
            return "cancelled", segments, tools_ran
        if not turn.tool_calls:
            return ("limit" if last else "done"), segments, tools_ran
        with _db() as db:
            store.save_assistant_tool_calls(db, run_id, _openai_tool_calls(turn), turn.text or "",
                                            user_id=ctx.user_id)
        messages.append({"role": "assistant", "content": turn.text or None, "tool_calls": _openai_tool_calls(turn)})
        for call in turn.tool_calls:
            cid, name = _call_id(call), _call_name(call)
            if _cancel_requested(run_id):
                return "cancelled", segments, tools_ran
            if used >= settings.AGENT_MAX_TOOL_CALLS or time.monotonic() > deadline:
                for_model, ok, summary = BUDGET_REFUSED, False, "Tool budget used up"
            else:
                used += 1
                for_model, ok, summary = _dispatch(ctx, call, emit)
                tools_ran.append(name)
            with _db() as db:
                store.save_tool_result(db, run_id, cid, name, for_model, ok=ok, summary=summary,
                                       user_id=ctx.user_id)
            messages.append({"role": "tool", "tool_call_id": cid, "content": for_model})
        with _db() as db:
            store.add_usage(db, run_id, tool_calls=len(turn.tool_calls))
    return "limit", segments, tools_ran


def _ensure_reply(emit: Emitter, ctx: Any, messages: list[dict], status: str,
                  segments: list[str], tools_ran: list[str]) -> None:
    """A turn that used tools must still end with words (design 4.4)."""
    if segments or status == "cancelled":
        return
    if tools_ran:
        turn = _model_step(emit, emit.run_id, messages + [{"role": "system", "content": prompt.EMPTY_REPLY_LINE}],
                           with_tools=False)
        emit.take_text()
        text = (strip_dashes(turn.text) or "").strip()
        if text:
            segments.append(text)
            return
    segments.append(prompt.FALLBACK_REPLY)


def run_turn(run_id: int, q: Any, slot: Any = None, *, user_message_id: Optional[int] = None,
             confirmation: Optional[Any] = None, persist: Optional[Any] = None) -> None:
    """Run one turn. Thread entry point: never raises. Always releases the slot
    and ends the queue with None."""
    emit = Emitter(run_id, q, persist=persist)
    beat = _Heartbeat(run_id).start()
    slot = slot or NullSlot()
    ctx: Any = None
    try:
        ctx = context.load(run_id)
        if user_message_id is None:
            user_message_id = _user_message_id(run_id)
        emit.emit({"type": "run_start", "run_id": run_id, "thread_id": int(ctx.thread_id),
                   "user_message_id": int(user_message_id or 0)})
        logger.info("[AGENT] run %s started", run_id, extra={"event": "agent_run_start"})
        if confirmation is not None:
            from services.agent import confirmations

            messages = build_messages(ctx)
            ack_line = confirmations.execute(ctx, confirmation, emit)
            turn = _model_step(emit, run_id, messages + [{"role": "system", "content": ack_line}], with_tools=False)
            emit.take_text()
            text = (strip_dashes(turn.text) or "").strip()
            status = "cancelled" if getattr(turn, "stopped", False) else "done"
            segments = [text] if text else ([] if status == "cancelled" else [prompt.FALLBACK_REPLY])
            _finish(emit, ctx, status, segments, ["update_profile_notes"])
            return
        messages = build_messages(ctx)
        status, segments, tools_ran = _loop(emit, ctx, messages)
        _ensure_reply(emit, ctx, messages, status, segments, tools_ran)
        _finish(emit, ctx, status, segments, tools_ran)
    except llm.ContentFiltered:
        _fail(emit, "content_filter", "content filter", getattr(ctx, "user_id", None))
    except llm.ModelUnavailable:
        _fail(emit, "model_unavailable", "model unavailable", getattr(ctx, "user_id", None))
    except Exception as exc:
        # Logs carry the type only; agent_runs.error (internal, admin only) keeps the text.
        logger.warning("[AGENT] run %s failed: %s", run_id, type(exc).__name__, extra={"event": "agent_run_error"})
        logger.debug("[AGENT] run traceback", exc_info=True)
        _fail(emit, "internal", f"{type(exc).__name__}: {exc}"[:2000], getattr(ctx, "user_id", None))
    finally:
        beat.stop()
        try:
            slot.release()
        finally:
            if q is not None:
                q.put(None)


def _user_message_id(run_id: int) -> Optional[int]:
    from database.models import AgentMessage

    with _db() as db:
        row = (db.query(AgentMessage.id).filter(AgentMessage.run_id == run_id, AgentMessage.role == "user")
               .order_by(AgentMessage.id.asc()).first())
        return int(row[0]) if row else None
