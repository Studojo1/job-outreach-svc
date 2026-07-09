"""Live X (Twitter) hiring posts via the xAI Agent Tools API — no scraping, no cookies.

Why this exists: Google's index of X posts lags by months (a `site:x.com` search
returns 2023-era posts), and browser-scraping X server-side needs an authed
session that gets flagged. xAI's hosted `x_search` tool reads X live and returns
real post URLs + dates. Verified format (Jul 2026): POST /v1/responses with
tools=[{"type":"x_search"}]; the OLD `search_parameters` Live Search API is
deprecated, and the `grok-4`/`grok-3` model names are gone (410) — use grok-4.3.

Key-gated: no-op (returns []) unless XAI_API_KEY is configured. Each call costs
real tokens (~$0.05-0.15), so runner cadence should stay daily, not per-request.

Returns the standard Mesa source dict shape plus post extras:
    {external_id, title, company, location, posted_date, url, author, apply_link, post_text}
"""

import hashlib
import json
import logging
import re

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_URL = "https://api.x.ai/v1/responses"
_TIMEOUT = 150.0

_INSTR = ("You extract REAL hiring posts from X. Be strict and factual; never invent posts, "
          "companies, or links. EXCLUDE course-sellers, coaching/mentorship accounts, and "
          "job-board aggregator accounts that repackage listings. Only genuine hiring posts "
          "by the company itself, its employees, or its founders.")


def _prompt(keywords: str, location: str) -> str:
    where = f" in or near {location}, or remote" if location else ""
    return (f"Search X for posts from the last 7 days where a company or founder is hiring: "
            f"{keywords}{where}. For each real post return an object with keys: company, role, "
            f"poster_handle, location, stipend_or_salary (or null), apply_link (or null), "
            f"post_url (the direct X status URL), post_date (YYYY-MM-DD). "
            f"Return ONLY a JSON array, up to 30 items. If none, return [].")


def _message_text(resp: dict) -> str:
    out = ""
    for item in resp.get("output", []):
        if item.get("type") == "message":
            content = item.get("content")
            if isinstance(content, list):
                out += "".join(part.get("text", "") for part in content if isinstance(part, dict))
            elif isinstance(content, str):
                out += content
    return out


def scrape_x_posts(keywords: str, location: str = "", date_posted: str = "week",
                   workplace_types=None, experience_levels=None, max_results: int = 30) -> list[dict]:
    """Mesa-source signature. One xAI agent call per invocation; [] on any failure."""
    api_key = (getattr(settings, "XAI_API_KEY", "") or "").strip()
    if not api_key:
        logger.info("[MESA_X] XAI_API_KEY not configured — skipping X source")
        return []
    body = {
        "model": (getattr(settings, "XAI_MODEL", "") or "grok-4.3").strip() or "grok-4.3",
        "instructions": _INSTR,
        "input": _prompt(keywords, location),
        "tools": [{"type": "x_search"}],
    }
    try:
        r = httpx.post(_URL, json=body, timeout=_TIMEOUT,
                       headers={"Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json"})
        if r.status_code != 200:
            logger.warning("[MESA_X] xAI HTTP %d: %s", r.status_code, r.text[:200])
            return []
        text = _message_text(r.json())
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA_X] xAI call failed: %s", e)
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        logger.info("[MESA_X] no JSON array in response for %r", keywords)
        return []
    try:
        items = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        logger.warning("[MESA_X] unparseable JSON for %r", keywords)
        return []
    rows: list[dict] = []
    for it in items[:max_results]:
        if not isinstance(it, dict):
            continue
        company = (it.get("company") or "").strip()
        post_url = (it.get("post_url") or "").strip()
        if not company or "/status/" not in post_url:
            continue  # a hiring row without a real X permalink is not evidence
        handle = (it.get("poster_handle") or "").strip().lstrip("@")
        rows.append({
            "external_id": "x_" + hashlib.sha1(post_url.encode()).hexdigest()[:20],
            "title": (it.get("role") or "Hiring post").strip(),
            "company": company,
            "location": (it.get("location") or location or "").strip(),
            "posted_date": (it.get("post_date") or "")[:10],
            "url": post_url.split("?")[0],
            "author": handle,
            "apply_link": (it.get("apply_link") or "").strip(),
            "post_text": (it.get("stipend_or_salary") or "") and f"Stipend/salary: {it['stipend_or_salary']}",
        })
    logger.info("[MESA_X] %r -> %d live X hiring posts", keywords, len(rows))
    return rows
