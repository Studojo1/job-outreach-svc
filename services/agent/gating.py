"""Who may use the student agent, and how much (contract section 2).

Order on every route except bootstrap: the AGENT_DISABLED kill switch (read
from the environment per request, so `kubectl set env` takes effect on the
next call), then AGENT_ENABLED, then the allowlist. Bootstrap asks
`gate_reason` instead and answers 200 `BootstrapDisabled`.

Also here: the error helper every agent route raises through, the IST day used
by the daily caps, the per-user and per-thread live-run checks, and the pod's
run slots (a bounded semaphore; each live run holds a drain thread and a
worker thread out of AnyIO's 40).
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

from core.config import settings
from services.agent.schemas import ErrorDetail

IST = timezone(timedelta(hours=5, minutes=30))
MAX_LIVE_RUNS_PER_USER = 2
SERVER_ERROR = "Something went wrong on our side. Please try again."

MESSAGES = {
    "agent_disabled": "The assistant is switched off right now. Your matches and drafts are safe.",
    "agent_not_enabled": "The assistant is not available on your account yet.",
    "not_found": "I couldn't find that. It may have been removed.",
    "run_in_progress": "I'm still working on your last message. It will finish in a moment.",
    "duplicate_turn": "That message is already being answered.",
    "daily_limit": "You've reached today's limit for the assistant. It resets at midnight, India time.",
    "busy": "I'm busy right now. Please try again in a minute.",
    "wrong_state": "That can't be done to this one right now.",
    "ticket_expired": "That request expired. Ask me again.",
    "invalid": "That request didn't look right. Please try again.",
}
STATUS_FOR = {
    "agent_disabled": 503, "agent_not_enabled": 403, "not_found": 404, "run_in_progress": 409,
    "duplicate_turn": 409, "daily_limit": 429, "draft_limit": 429, "busy": 503,
    "wrong_state": 409, "version_conflict": 409, "invalid": 422,
}


def agent_error(code: str, message: Optional[str] = None, status: Optional[int] = None,
                **extra: Any) -> HTTPException:
    """An HTTPException whose body is the contract's ErrorResponse."""
    current = extra.pop("current", None)
    if current is not None and hasattr(current, "model_dump"):
        current = current.model_dump(mode="json")
    detail = ErrorDetail(code=code, message=message or MESSAGES.get(code, SERVER_ERROR),
                         current=current, **{k: v for k, v in extra.items() if k in ("run_id", "limit", "resets_at")})
    return HTTPException(status_code=status or STATUS_FOR.get(code, 400), detail=detail.model_dump(mode="json"))


# ---------------------------------------------------------------- the gate

def kill_switch_on() -> bool:
    return os.getenv("AGENT_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _allowlist() -> list[str]:
    raw = settings.AGENT_ALLOWLIST or ""
    return [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]


def user_allowed(user: Any) -> bool:
    if not settings.AGENT_ENABLED:
        return False
    entries = _allowlist()
    if not entries:
        return False
    if "*" in entries:
        return True
    ids = {str(getattr(user, "id", "") or "").strip().lower()}
    # An email entry counts only once the address is verified, so nobody can
    # claim an allowlisted address by signing up with it first.
    if getattr(user, "email_verified", False):
        ids.add(str(getattr(user, "email", "") or "").strip().lower())
    ids.discard("")
    return any(e in ids for e in entries)


def gate_reason(user: Any) -> Optional[str]:
    """None when the user may use the agent, else the BootstrapDisabled reason."""
    if kill_switch_on():
        return "agent_disabled"
    if not user_allowed(user):
        return "agent_not_enabled"
    return None


def require_enabled(user: Any) -> None:
    reason = gate_reason(user)
    if reason:
        raise agent_error(reason)


# ---------------------------------------------------------------- the IST day

def ist_today(now: Optional[datetime] = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(IST).date()


def next_ist_midnight_utc(now: Optional[datetime] = None) -> str:
    """The next IST midnight as a naive UTC ISO string (the caps reset then)."""
    day = ist_today(now) + timedelta(days=1)
    midnight = datetime(day.year, day.month, day.day, tzinfo=IST).astimezone(timezone.utc)
    return midnight.replace(tzinfo=None).isoformat(timespec="seconds")


def _get(obj: Any, name: str, default: int = 0) -> int:
    if obj is None:
        return default
    value = obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
    return int(value or 0)


def usage_numbers(usage: Any) -> dict:
    return {k: _get(usage, k) for k in ("messages", "drafts", "revisions", "tokens")}


def check_daily_caps(usage: Any) -> None:
    """429 daily_limit when today's messages or tokens are used up."""
    u = usage_numbers(usage)
    if u["messages"] >= settings.AGENT_DAILY_MESSAGE_CAP:
        raise agent_error("daily_limit", limit="messages", resets_at=next_ist_midnight_utc())
    if u["tokens"] >= settings.AGENT_DAILY_TOKEN_CAP:
        raise agent_error("daily_limit", limit="tokens", resets_at=next_ist_midnight_utc())


def check_live_runs(live_runs: list, thread_id: int) -> None:
    """409 run_in_progress for a live run on this thread, or two for the user."""
    for run in live_runs:
        if int(getattr(run, "thread_id", 0) or 0) == int(thread_id):
            raise agent_error("run_in_progress", run_id=int(run.id))
    if len(live_runs) >= MAX_LIVE_RUNS_PER_USER:
        newest = max(live_runs, key=lambda r: int(r.id))
        raise agent_error("run_in_progress",
                          message="You have two conversations working already. Let one finish first.",
                          run_id=int(newest.id))


# ---------------------------------------------------------------- pod run slots

class Slot:
    """One held run slot. `release` is idempotent."""

    def __init__(self, sem: threading.BoundedSemaphore):
        self._sem = sem
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._sem.release()


class NullSlot:
    def release(self) -> None:
        return None


_sem_lock = threading.Lock()
_sem: Optional[threading.BoundedSemaphore] = None


def _semaphore() -> threading.BoundedSemaphore:
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.BoundedSemaphore(max(1, int(settings.AGENT_MAX_CONCURRENT_RUNS)))
        return _sem


def acquire_slot() -> Slot:
    """Take a run slot without waiting, or raise 503 busy."""
    sem = _semaphore()
    if not sem.acquire(blocking=False):
        raise agent_error("busy")
    return Slot(sem)


def reset_slots_for_tests() -> None:
    global _sem
    with _sem_lock:
        _sem = None
