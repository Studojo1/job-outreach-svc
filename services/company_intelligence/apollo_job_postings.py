"""Apollo job-postings fetcher.

For each top-K company, pull active job postings (last 30 days) so the
fact-extractor + justifier know what the company is actually hiring for —
which is the strongest possible signal of "they want someone like you".

Apollo exposes job postings via several endpoints depending on plan:
1. GET /api/v1/organizations/{id}/job_postings (per-org listing)
2. POST /api/v1/jobs/search with organization_ids[] filter

We try (1) first because it's cheap (no scoring/ranking, just a fetch).
On 4xx (plan doesn't expose it) we silently return empty — discovery
must continue, this is enrichment, not core data.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from core.config import settings

logger = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
JOB_POSTINGS_TIMEOUT_SEC = 12
MAX_POSTINGS_PER_COMPANY = 5
RECENT_DAYS = 30


def fetch_job_postings(apollo_org_id: str) -> List[Dict[str, Any]]:
    """Fetch active job postings for a single Apollo org. Returns at most
    MAX_POSTINGS_PER_COMPANY entries, each {title, snippet, posted_at}.

    Returns empty list (not None) on any failure — this is non-critical
    enrichment, the discovery pipeline must keep moving.
    """
    if not apollo_org_id:
        return []
    if not settings.APOLLO_API_KEY or settings.APOLLO_API_KEY == "your_apollo_key_here":
        return []

    url = f"{APOLLO_BASE_URL}/organizations/{apollo_org_id}/job_postings"
    headers = {
        "Cache-Control": "no-cache",
        "X-Api-Key": settings.APOLLO_API_KEY,
    }

    # Same throttle as people-search to stay under Apollo's rate limits.
    time.sleep(0.2)

    try:
        resp = requests.get(url, headers=headers, timeout=JOB_POSTINGS_TIMEOUT_SEC)
    except requests.RequestException as e:
        logger.warning("[APOLLO_JOBS] network error for org %s: %s", apollo_org_id, e)
        return []

    if resp.status_code == 404:
        # Apollo returns 404 when an org has no public postings — silent.
        return []
    if resp.status_code in (401, 403):
        # Plan doesn't expose this endpoint. Log once-per-process would be
        # ideal but a per-call info log is fine for now.
        logger.info("[APOLLO_JOBS] endpoint not authorized (HTTP %d) for org %s", resp.status_code, apollo_org_id)
        return []
    if not resp.ok:
        logger.warning("[APOLLO_JOBS] HTTP %d for org %s: %s", resp.status_code, apollo_org_id, resp.text[:200])
        return []

    try:
        body = resp.json()
    except ValueError:
        logger.warning("[APOLLO_JOBS] non-JSON for org %s", apollo_org_id)
        return []

    raw_postings = body.get("job_postings") or body.get("data") or body.get("results") or []
    if not isinstance(raw_postings, list):
        return []

    cutoff = datetime.utcnow() - timedelta(days=RECENT_DAYS)
    cleaned: List[Dict[str, Any]] = []
    for posting in raw_postings:
        if not isinstance(posting, dict):
            continue
        title = (posting.get("title") or posting.get("job_title") or "").strip()
        if not title:
            continue
        posted_at_raw = posting.get("posted_at") or posting.get("created_at")
        posted_at = _parse_dt(posted_at_raw)
        if posted_at and posted_at < cutoff:
            continue
        snippet_src = (
            posting.get("description")
            or posting.get("description_text")
            or posting.get("snippet")
            or ""
        )
        snippet = " ".join(str(snippet_src).split())[:200] or None
        cleaned.append({
            "title": title[:120],
            "snippet": snippet,
            "posted_at": posted_at_raw[:10] if isinstance(posted_at_raw, str) else None,
            "url": posting.get("url"),
        })
        if len(cleaned) >= MAX_POSTINGS_PER_COMPANY:
            break

    logger.info("[APOLLO_JOBS] org %s — kept %d/%d postings", apollo_org_id, len(cleaned), len(raw_postings))
    return cleaned


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        # Apollo returns ISO strings; tolerate the trailing Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None
