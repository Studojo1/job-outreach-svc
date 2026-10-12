"""Stream frames for the student agent: numbering, validation, persistence, SSE.

Contract section 4. Every frame is one `schemas_chat.StreamEvent`, numbered with
a per-run `seq` from 1. Every frame except `text` is appended to
`agent_runs.events` BEFORE it is queued, so `GET /agent/runs/{id}?after_seq=N`
on any pod replays exactly what the live stream showed (minus text deltas,
which `message_end.content` replaces anyway).

`strip_dashes` is the product rule "no em or en dashes anywhere": it runs on
every model text delta, the final text, every draft and every revision. It
mirrors the frontend's `app/lib/strip-dashes.ts`.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from typing import Any, Callable, Optional

from pydantic import TypeAdapter

from core.logger import get_logger
from services.agent.schemas_chat import StreamEvent

logger = get_logger(__name__)

PING = ": ping\n\n"

_EVENT_ADAPTER: TypeAdapter = TypeAdapter(StreamEvent)

_DASH_CHARS = ("\u2013", "\u2014")
_RANGE = re.compile(r"(?<=\d)[ \t]*[\u2013\u2014][ \t]*(?=\d)")
_DASH = re.compile(r"[ \t]*[\u2013\u2014]+[ \t]*")
_COMMA_BEFORE_PUNCT = re.compile(r", ([,.;:!?)])")
_COMMA_OPENING_LINE = re.compile(r"(^|\n), ")
_COMMA_BEFORE_BREAK = re.compile(r", (?=\n)")
_TRAILING_COMMA = re.compile(r",\s*$")


def strip_dashes(text: Optional[str], partial: bool = False) -> Optional[str]:
    """Replace em and en dashes. Numeric ranges become a hyphen (2-5), every
    other dash becomes a comma. Text without a dash is returned untouched.

    `partial=True` is for a streamed delta: only the replacement itself runs,
    because the start or end of a delta is not the start or end of the text
    (a delta that ends in a real comma must keep it).
    """
    if not text or not any(c in text for c in _DASH_CHARS):
        return text
    out = _RANGE.sub("-", text)
    out = _DASH.sub(", ", out)
    out = _COMMA_BEFORE_PUNCT.sub(r"\1", out)
    if partial:
        return out
    out = _COMMA_OPENING_LINE.sub(r"\1", out)
    out = _COMMA_BEFORE_BREAK.sub(",", out)
    return _TRAILING_COMMA.sub("", out)


def strip_dashes_deep(value: Any) -> Any:
    """strip_dashes over every string inside dicts and lists."""
    if isinstance(value, str):
        return strip_dashes(value)
    if isinstance(value, list):
        return [strip_dashes_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: strip_dashes_deep(v) for k, v in value.items()}
    return value


def sse(frame: dict) -> str:
    """One SSE frame: `data: {json}` and a blank line."""
    return "data: " + json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def validate(frame: dict) -> dict:
    """Validate a frame against the StreamEvent union and return its canonical
    JSON form (every field present, `null` when empty)."""
    model = _EVENT_ADAPTER.validate_python(frame)
    return model.model_dump(mode="json")


def valid_frames(frames: list) -> list[dict]:
    """Stored frames that still match the StreamEvent union (a stale shape is
    dropped, never fatal for a poll)."""
    out = []
    for f in frames or []:
        try:
            out.append(validate(f))
        except Exception:
            continue
    return out


def drain(q: "queue.Queue", ping_seconds: float = 15):
    """The SSE body: frames from the worker's queue, `: ping` while it is quiet,
    until the closing None. A sync generator (Starlette iterates it on the
    threadpool); a dropped connection closes only this generator, and the
    worker still finishes and persists."""
    while True:
        try:
            frame = q.get(timeout=ping_seconds)
        except queue.Empty:
            yield PING
            continue
        if frame is None:
            return
        yield sse(frame)


def _persist_default(run_id: int, frame: dict) -> None:
    """Append one frame to agent_runs.events in its own short session."""
    from services.agent import context, store

    db = context.open_session()
    try:
        store.append_event(db, run_id, frame)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class Emitter:
    """Numbers, validates, persists and queues the frames of one run.

    Thread safe: the draft executor's `on_done` callback may emit from another
    thread, so numbering, persistence and queueing happen under one lock and
    the persisted order always equals the seq order.

    `persist(run_id, frame)` defaults to a store write in its own session; tests
    pass their own. `start_seq` lets a resumed writer (the confirmation path,
    the reaper) continue a run's numbering.
    """

    def __init__(self, run_id: int, q: Any = None,
                 persist: Optional[Callable[[int, dict], None]] = None, start_seq: int = 0):
        self.run_id = run_id
        self.q = q
        self.seq = start_seq
        self._persist = persist or _persist_default
        self._lock = threading.Lock()
        self.frames: list[dict] = []            # persisted frames, in order
        self.text_parts: list[str] = []         # text of the current model step
        self.closed = False

    def emit(self, event: dict) -> int:
        with self._lock:
            seq = self.seq + 1
            frame = validate({**event, "seq": seq})
            if frame["type"] != "text":
                self._persist(self.run_id, frame)
                self.frames.append(frame)
            self.seq = seq
            if self.q is not None and not self.closed:
                self.q.put(frame)
            return seq

    def text(self, delta: str) -> None:
        """Stream a text delta (dash stripped). Empty deltas are dropped."""
        if not delta:
            return
        clean = strip_dashes(delta, partial=True) or ""
        if not clean:
            return
        self.text_parts.append(clean)
        self.emit({"type": "text", "delta": clean})

    def take_text(self) -> str:
        """The text streamed since the last call, joined."""
        joined = "".join(self.text_parts)
        self.text_parts = []
        return joined

    # Views over what was emitted, for the final message meta and `done`.

    def of_type(self, *types: str) -> list[dict]:
        return [f for f in self.frames if f["type"] in types]

    def invalidate_union(self) -> list[str]:
        seen: list[str] = []
        for f in self.frames:
            for key in f.get("invalidate") or []:
                if key not in seen:
                    seen.append(key)
        return seen
