"""Apollo API key manager with automatic fallback on credit exhaustion.

Holds an ordered list of API keys. When a request returns 402 (payment
required) or 401 (unauthorized), the key is marked exhausted and the next
key in the list is used. Thread-safe.

An exhausted key used to stay exhausted until the pod restarted, so topping
up Apollo recovered nothing on its own. Now a mark expires after
EXHAUSTED_RETRY_AFTER: the next call is a single probe that either works
(credits are back) or re-marks the key. reset() clears every mark at once,
for when the account has just been topped up.
"""

import threading
import time
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger(__name__)

_EXHAUSTED_STATUS_CODES = {401, 402, 403}
EXHAUSTED_RETRY_AFTER = 30 * 60  # seconds before an exhausted key is probed again


class ApolloKeysExhausted(RuntimeError, ValueError):
    """Every Apollo key is out of credits. Recoverable once the account is topped up.

    A RuntimeError so enrichment classifies it as credit_exhausted; still a
    ValueError because that is what _request used to raise.
    """


class _ApolloKeyManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: list[str] = []
        self._exhausted: dict[str, float] = {}  # key -> monotonic time it was marked
        self._loaded = False

    def _load(self) -> None:
        from core.config import settings

        keys: list[str] = []
        for attr in ("APOLLO_API_KEY", "APOLLO_API_KEY_2", "APOLLO_API_KEY_3"):
            val = (getattr(settings, attr, None) or "").strip()
            if val and val not in ("your_apollo_key_here",) and val not in keys:
                keys.append(val)
        self._keys = keys
        logger.info("[ApolloKeys] Loaded %d API key(s)", len(keys))

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._load()
                    self._loaded = True

    def _is_exhausted(self, key: str) -> bool:
        """Caller holds the lock. Drops the mark once it is old enough to re-probe."""
        marked_at = self._exhausted.get(key)
        if marked_at is None:
            return False
        if time.monotonic() - marked_at >= EXHAUSTED_RETRY_AFTER:
            del self._exhausted[key]
            logger.info("[ApolloKeys] Key ...%s: retrying after %ds", key[-6:], EXHAUSTED_RETRY_AFTER)
            return False
        return True

    def get_key(self) -> Optional[str]:
        """Return the first non-exhausted key, or None if all are exhausted."""
        self._ensure_loaded()
        with self._lock:
            for key in self._keys:
                if not self._is_exhausted(key):
                    return key
        return None

    def report_failure(self, key: str, status_code: int) -> None:
        """Mark a key exhausted after a 402/401/403 response."""
        if status_code not in _EXHAUSTED_STATUS_CODES:
            return
        with self._lock:
            if key in self._exhausted:
                return
            self._exhausted[key] = time.monotonic()
            remaining = sum(1 for k in self._keys if k not in self._exhausted)
            logger.warning(
                "[ApolloKeys] Key ...%s exhausted (HTTP %d). %d key(s) still active.",
                key[-6:], status_code, remaining,
            )

    def has_valid_key(self) -> bool:
        self._ensure_loaded()
        return self.get_key() is not None

    def reset(self) -> None:
        """Forget every exhaustion mark (the account was just topped up)."""
        with self._lock:
            if self._exhausted:
                logger.info("[ApolloKeys] Reset %d exhausted key(s)", len(self._exhausted))
            self._exhausted.clear()


apollo_keys = _ApolloKeyManager()


# ── Drop-in helpers for callers ────────────────────────────────────────────

def apollo_get(url: str, **kwargs) -> requests.Response:
    """GET with automatic key rotation on exhaustion. Raises on final failure."""
    return _request("GET", url, **kwargs)


def apollo_post(url: str, **kwargs) -> requests.Response:
    """POST with automatic key rotation on exhaustion. Raises on final failure."""
    return _request("POST", url, **kwargs)


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Internal: try each available key in order, rotating on 402/401/403."""
    apollo_keys._ensure_loaded()
    tried: set[str] = set()

    while True:
        key = apollo_keys.get_key()
        if key is None:
            raise ApolloKeysExhausted(
                "All Apollo API keys are exhausted. Add more credits or a new key."
            )
        if key in tried:
            # We've looped — shouldn't happen, but guard against infinite loop.
            raise ApolloKeysExhausted("All Apollo API keys exhausted after retry.")

        tried.add(key)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Api-Key"] = key
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Cache-Control", "no-cache")

        resp = requests.request(method, url, headers=headers, **kwargs)

        if resp.status_code in _EXHAUSTED_STATUS_CODES:
            apollo_keys.report_failure(key, resp.status_code)
            # Loop to try next key without raising.
            continue

        return resp
