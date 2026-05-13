"""Apollo API key manager with automatic fallback on credit exhaustion.

Holds an ordered list of API keys. When a request returns 402 (payment
required) or 401 (unauthorized), the key is marked exhausted and the next
key in the list is used. Thread-safe; state resets on pod restart.
"""

import threading
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger(__name__)

_EXHAUSTED_STATUS_CODES = {401, 402, 403}


class _ApolloKeyManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: list[str] = []
        self._exhausted: set[str] = set()
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

    def get_key(self) -> Optional[str]:
        """Return the first non-exhausted key, or None if all are exhausted."""
        self._ensure_loaded()
        with self._lock:
            for key in self._keys:
                if key not in self._exhausted:
                    return key
        return None

    def report_failure(self, key: str, status_code: int) -> None:
        """Mark a key exhausted after a 402/401/403 response."""
        if status_code not in _EXHAUSTED_STATUS_CODES:
            return
        with self._lock:
            if key in self._exhausted:
                return
            self._exhausted.add(key)
            remaining = sum(1 for k in self._keys if k not in self._exhausted)
            logger.warning(
                "[ApolloKeys] Key ...%s exhausted (HTTP %d). %d key(s) still active.",
                key[-6:], status_code, remaining,
            )

    def has_valid_key(self) -> bool:
        self._ensure_loaded()
        return self.get_key() is not None


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
            raise ValueError(
                "All Apollo API keys are exhausted. Add more credits or a new key."
            )
        if key in tried:
            # We've looped — shouldn't happen, but guard against infinite loop.
            raise ValueError("All Apollo API keys exhausted after retry.")

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
