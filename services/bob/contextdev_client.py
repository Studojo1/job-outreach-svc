"""Context.dev client for Bob — credit-disciplined evidence retrieval.

Credit economics (verified against docs.context.dev, July 2026):
  - POST /v1/web/search        → 1 credit per 10 results, markdown scraping INCLUDED.
  - GET  /v1/web/scrape/markdown → 1 credit per call (single URL, surgical use only).
  - Brand / Extract / Classification endpoints are NEVER used — poor credit
    input/output ratio for our use case (several focused 1-credit searches beat
    one 10-credit brand lookup).

Cost controls:
  - DB-backed evidence cache (bob_evidence_cache) with TTL by freshness — a
    repeated query within TTL costs 0 credits.
  - markdownOptions.maxAgeMs set high so Context.dev serves its own cache fast.
  - Every live call records credits_consumed; callers enforce per-run budgets.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from sqlalchemy import text

from core.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.context.dev/v1"
_TIMEOUT = 150  # markdown-enabled searches can be slow

# Our DB cache TTL per freshness window (Context.dev results within these
# windows are stable enough to reuse without paying again).
_CACHE_TTL = {
    "last_24_hours": timedelta(hours=6),
    "last_week": timedelta(days=1),
    "last_month": timedelta(days=3),
    "last_year": timedelta(days=7),
    None: timedelta(days=14),  # no freshness constraint = mostly static facts
}


class ContextDevError(Exception):
    pass


def _headers() -> dict:
    key = settings.CONTEXT_DEV_API_KEY
    if not key:
        raise ContextDevError("CONTEXT_DEV_API_KEY is not configured")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _cache_key(kind: str, payload: dict) -> str:
    canon = json.dumps({"kind": kind, **payload}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def _cache_get(db, key: str) -> Optional[dict]:
    row = db.execute(
        text("SELECT payload FROM bob_evidence_cache WHERE cache_key = :k AND (expires_at IS NULL OR expires_at > now())"),
        {"k": key},
    ).fetchone()
    return row[0] if row else None


def _cache_put(db, key: str, kind: str, request: dict, payload: dict, credits: int, ttl: timedelta) -> None:
    try:
        db.execute(
            text(
                "INSERT INTO bob_evidence_cache (cache_key, kind, request, payload, credits_used, expires_at) "
                "VALUES (:k, :kind, CAST(:req AS jsonb), CAST(:payload AS jsonb), :credits, :exp) "
                "ON CONFLICT (cache_key) DO UPDATE SET payload = CAST(:payload AS jsonb), credits_used = :credits, "
                "created_at = now(), expires_at = :exp"
            ),
            {
                "k": key,
                "kind": kind,
                "req": json.dumps(request),
                "payload": json.dumps(payload),
                "credits": credits,
                "exp": datetime.now(timezone.utc) + ttl,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("[BOB/CTX] cache write failed", exc_info=True)


def web_search(
    db,
    query: str,
    num_results: int = 10,
    freshness: Optional[str] = None,
    country: Optional[str] = "IN",
    fanout: bool = False,
    include_domains: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Search the web with markdown content included. 1 credit / 10 results.

    Returns {"results": [...], "credits_consumed": int, "cached": bool}.
    """
    num_results = max(10, min(int(num_results or 10), 40))  # cap at 40 = 4 credits
    body: dict[str, Any] = {
        "query": query[:500],
        "numResults": num_results,
        "markdownOptions": {
            "enabled": True,
            "useMainContentOnly": True,
            "includeLinks": True,
            # Let Context.dev serve from its own scrape cache when possible.
            "maxAgeMs": 3 * 24 * 60 * 60 * 1000,
        },
    }
    if freshness in ("last_24_hours", "last_week", "last_month", "last_year"):
        body["freshness"] = freshness
    if country:
        body["country"] = country
    if fanout:
        body["queryFanout"] = True
    if include_domains:
        body["includeDomains"] = include_domains[:20]

    key = _cache_key("search", body)
    cached = _cache_get(db, key)
    if cached is not None:
        return {"results": cached.get("results", []), "credits_consumed": 0, "cached": True}

    resp = requests.post(f"{_BASE}/web/search", headers=_headers(), json=body, timeout=_TIMEOUT)
    if not resp.ok:
        raise ContextDevError(f"web/search {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    credits = int((data.get("key_metadata") or {}).get("credits_consumed") or 0)
    remaining = (data.get("key_metadata") or {}).get("credits_remaining")
    results = _compact_results(data.get("results") or [])
    logger.info(
        "[BOB/CTX] search ok results=%d credits=%d remaining=%s q=%r",
        len(results), credits, remaining, query[:80],
    )
    _cache_put(db, key, "search", body, {"results": results}, credits,
               _CACHE_TTL.get(body.get("freshness"), _CACHE_TTL[None]))
    return {"results": results, "credits_consumed": credits, "cached": False,
            "credits_remaining": remaining}


def scrape_markdown(db, url: str) -> dict[str, Any]:
    """Scrape one URL to markdown. 1 credit. Surgical use only."""
    params = {
        "url": url,
        "useMainContentOnly": "true",
        "includeLinks": "true",
        "maxAgeMs": str(7 * 24 * 60 * 60 * 1000),
    }
    key = _cache_key("scrape", {"url": url})
    cached = _cache_get(db, key)
    if cached is not None:
        return {"markdown": cached.get("markdown", ""), "credits_consumed": 0, "cached": True}

    resp = requests.get(f"{_BASE}/web/scrape/markdown", headers=_headers(), params=params, timeout=_TIMEOUT)
    if not resp.ok:
        raise ContextDevError(f"scrape/markdown {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    credits = int((data.get("key_metadata") or {}).get("credits_consumed") or 1)
    md = (data.get("markdown") or "")[:40000]
    _cache_put(db, key, "scrape", {"url": url}, {"markdown": md}, credits, _CACHE_TTL[None])
    logger.info("[BOB/CTX] scrape ok credits=%d url=%s", credits, url[:120])
    return {"markdown": md, "credits_consumed": credits, "cached": False}


def _compact_results(results: list[dict]) -> list[dict]:
    """Keep only fields the agent needs; trim markdown to control tokens."""
    out = []
    for r in results:
        md_block = r.get("markdown") or {}
        md = md_block.get("markdown") if isinstance(md_block, dict) else None
        out.append({
            "url": r.get("url"),
            "title": r.get("title"),
            "description": (r.get("description") or "")[:300],
            "relevance": r.get("relevance"),
            "markdown": (md or "")[:4000],
            "scrape_code": md_block.get("code") if isinstance(md_block, dict) else None,
        })
    return out
