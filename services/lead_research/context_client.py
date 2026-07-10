"""context.dev client — three aimed calls per lead, 3 credits maximum.

A credit is charged PER CALL, not per URL. /web/search returns ten results, and
with markdownOptions.enabled it scrapes all ten inline for the same one credit.
So the game is not spending less; it is aiming the ten.

Call 1  LinkedIn, snippets only        (1 credit)
Call 2  YouTube/Medium/Substack, scraped (1 credit)
Call 3  everything else, inline-scraped(1 credit)

Two rules are load-bearing:

  * Call 1 leaves markdown OFF. Not as an economy. LinkedIn serves a login wall
    to any anonymous fetcher, so inline scraping returns zero-byte markdown for
    every URL, every time. The search engine's own description snippets come
    back full and cost nothing extra, and they carry real signal. Any
    linkedin.com/posts/ URLs that surface are harvested for Call 3 — post pages,
    unlike profile pages, do render completely.

  * Call 3 never refetches a URL Call 2 already scraped, and is skipped entirely
    when Calls 1 and 2 already yielded enough. A two-credit lead is a good lead.

Reddit is deliberately excluded from Call 2 and allowed only in Call 3. Reddit is
pseudonymous: searching a lead's name there returns a different human whose
opinions would then be attributed to your lead. That is not thin data, it is
wrong data, and it is the one failure mode that damages an email rather than
merely weakening it. In Call 3 a hit can be sanity-checked against the fuller
picture before it is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)

# Measured against the live API: a bare search returns in ~2s, but a search with
# markdownOptions.enabled scrapes all ten results inline and took 48.2s. A 20s
# timeout guaranteed that Calls 2 and 3 could never succeed.
_TIMEOUT_SEARCH = 30   # Call 1: snippets only
_TIMEOUT_SCRAPE = 120  # Calls 2 and 3: ten pages fetched inline

# Call 2's territory: where a person's own long-form words actually live.
#
# X was the original choice because identity resolves there. Measured against the
# live API on two leads, YouTube out-returned X (10 results vs 8) on the only lead
# with any public footprint, and X returned nothing for the silent one while still
# charging its credit. Founder podcasts (Elevation, YourStory, 100x) are YouTube
# artifacts, and YouTube has no login wall. Medium/Substack catch the ones who write.
_CALL2_INCLUDE = ["youtube.com", "medium.com", "substack.com"]

# Never re-scrape what Calls 1 and 2 already own.
_CALL3_EXCLUDE = ["linkedin.com"] + _CALL2_INCLUDE

# Per-URL scrape outcomes returned inside each result's `markdown` object.
_MD_SUCCESS = "SUCCESS"


class ContextDevError(Exception):
    """Transient failure — caller should leave the lead pending and retry."""


class ContextDevAuthError(ContextDevError):
    """Base for 'stop calling until a human intervenes'."""


class ContextDevCreditError(ContextDevAuthError):
    """402 — out of credits. Genuinely retryable once the account is topped up,
    so the caller drops its claim and lets the lead be picked up again later."""


class ContextDevConfigError(ContextDevAuthError):
    """401/403 — bad key, revoked key, or wrong base URL.

    NOT retryable. Retrying is a tight loop against a paid API: the worker polls
    every 30s, so a lead whose claim is released re-calls 120x/hour. And a
    rejected call can still be billed — a client-side timeout does not cancel
    server-side work. The caller must keep the claim so the lead is never retried.

    A wrong CONTEXT_DEV_BASE_URL returns exactly this:
        403 "The API you have tried to access does not exist."
    """


@dataclass
class ResearchBundle:
    """Raw material from context.dev. Extraction into layers happens elsewhere."""
    linkedin_snippets: list[str] = field(default_factory=list)
    linkedin_post_urls: list[str] = field(default_factory=list)
    x_markdown: list[str] = field(default_factory=list)
    web_markdown: list[str] = field(default_factory=list)
    fetched_urls: list[str] = field(default_factory=list)
    credits_spent: int = 0

    def has_signal(self) -> bool:
        return bool(
            self.linkedin_snippets or self.x_markdown or self.web_markdown
        )

    def is_rich(self) -> bool:
        """Enough from Calls 1+2 that Call 3 would be a wasted credit.

        Requires a LinkedIn *post* URL, not merely a Call-2 hit. Measured on a real
        lead: searching "Divakar Sharma" + Neeman's on Substack returned a 76k-char
        legal-judgments newsletter by someone else, matched purely on his compliance
        role. Treating any Call-2 document as "their own words" both (a) risks
        attributing a stranger's opinions to the lead and (b) skips Call 3, which is
        where that lead's real signal lived. Only the extractor can judge whether a
        document is actually theirs, and it runs later — so the cheap heuristic must
        be conservative and let Call 3 run.
        """
        return bool(self.x_markdown) and bool(self.linkedin_post_urls)


def _enabled() -> bool:
    return bool(settings.CONTEXT_DEV_API_KEY)


# Circuit breaker. A 401/403 means the key or the base URL is wrong for the whole
# process, not just this lead. Without this, the worker would keep calling a paid
# API once per lead per cycle. Cleared only by a restart (i.e. a config change).
_config_broken: str | None = None


def _post(path: str, payload: dict, timeout: int = _TIMEOUT_SEARCH) -> dict:
    global _config_broken
    if _config_broken:
        raise ContextDevConfigError(f"context.dev disabled this process: {_config_broken}")

    url = f"{settings.CONTEXT_DEV_BASE_URL.rstrip('/')}{path}"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.CONTEXT_DEV_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        # A client-side timeout does NOT cancel server-side work — the call may
        # still have been billed. Transient, so the lead is not retried.
        raise ContextDevError(f"context.dev network error: {e}") from e

    if resp.status_code == 402:
        raise ContextDevCreditError(f"context.dev out of credits: {resp.text[:200]}")

    if resp.status_code in (401, 403):
        # Misconfiguration, not a per-lead problem. Trip the breaker so we stop
        # hammering a paid endpoint on every subsequent lead.
        _config_broken = f"HTTP {resp.status_code}: {resp.text[:160]}"
        logger.error("[Research] context.dev config error, disabling until restart: %s",
                     _config_broken)
        raise ContextDevConfigError(_config_broken)

    if not resp.ok:
        raise ContextDevError(f"context.dev HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as e:
        raise ContextDevError(f"context.dev returned non-JSON: {e}") from e


def _results(body: dict) -> list[dict[str, Any]]:
    """context.dev returns the list under `results`. Tolerate `data`/`items` too."""
    for key in ("results", "data", "items"):
        val = body.get(key)
        if isinstance(val, list):
            return val
    return []


def _markdown(result: dict) -> str:
    """Extract scraped text from a result's `markdown` field.

    The field is an OBJECT, not a string:
        {"markdown": "# Title\\n...", "code": "SUCCESS"}
        {"markdown": None,          "code": "TIMEOUT"}
        {"markdown": None,          "code": "NOT_REQUESTED"}

    Treating it as a string (`(r.get("markdown") or "").strip()`) raises
    AttributeError: 'dict' object has no attribute 'strip' — on the very first
    scraped result. A per-URL TIMEOUT is normal (ZoomInfo returns it reliably);
    those yield no text and must be skipped, not counted as content.
    """
    md = result.get("markdown")
    if isinstance(md, str):          # tolerate a future flattening of the shape
        return md.strip()
    if not isinstance(md, dict):
        return ""
    if md.get("code") != _MD_SUCCESS:
        return ""
    return (md.get("markdown") or "").strip()


def _credits_used(body: dict, fallback: int = 1) -> int:
    """Trust the API's own accounting over a local counter when it is present."""
    meta = body.get("key_metadata")
    if isinstance(meta, dict) and isinstance(meta.get("credits_consumed"), int):
        return meta["credits_consumed"]
    return fallback


def _query_for(name: str, company: str, title: str = "") -> str:
    parts = [p for p in (f'"{name}"', company, title) if p]
    return " ".join(parts)


# ── Call 1: LinkedIn, snippets only ────────────────────────────────────────────

def _call_linkedin(bundle: ResearchBundle, name: str, company: str) -> None:
    """Markdown stays OFF: LinkedIn's login wall returns zero-byte markdown for
    every URL. The snippets are the payload."""
    body = _post("/web/search", {
        "query": _query_for(name, company),
        "includeDomains": ["linkedin.com"],
        "markdownOptions": {"enabled": False},
    }, timeout=_TIMEOUT_SEARCH)
    bundle.credits_spent += _credits_used(body)

    for r in _results(body):
        snippet = (r.get("description") or r.get("snippet") or "").strip()
        if snippet:
            bundle.linkedin_snippets.append(snippet)
        url = (r.get("url") or "").strip()
        if url:
            bundle.fetched_urls.append(url)
            # Post pages, unlike profile pages, render completely. Save for Call 3.
            if "/posts/" in url or "/pulse/" in url:
                bundle.linkedin_post_urls.append(url)

    logger.info("[Research] Call 1 (LinkedIn): %d snippets, %d post URLs",
                len(bundle.linkedin_snippets), len(bundle.linkedin_post_urls))


# ── Call 2: X, inline-scraped ──────────────────────────────────────────────────

def _call_x(bundle: ResearchBundle, name: str, company: str) -> None:
    """Their own long-form words: podcasts, talks, essays.

    Reddit stays out. It is pseudonymous: searching a lead's name there returns a
    different human whose opinions would then be attributed to your lead. That is
    not thin data, it is wrong data, and it is the one failure mode that damages an
    email rather than merely weakening it. Reddit is allowed only in Call 3, where a
    hit can be sanity-checked against the fuller picture.
    """
    body = _post("/web/search", {
        "query": _query_for(name, company),
        "includeDomains": _CALL2_INCLUDE,
        "markdownOptions": {"enabled": True},
    }, timeout=_TIMEOUT_SCRAPE)
    bundle.credits_spent += _credits_used(body)

    for r in _results(body):
        url = (r.get("url") or "").strip()
        if url:
            bundle.fetched_urls.append(url)
        md = _markdown(r)
        if md:
            bundle.x_markdown.append(md)

    logger.info("[Research] Call 2 (talks/essays): %d scraped docs", len(bundle.x_markdown))


# ── Call 3: the whole rest of the web ──────────────────────────────────────────

def _call_web(bundle: ResearchBundle, name: str, company: str, title: str) -> None:
    """Where the depth actually lives: podcast pages, case studies, RocketReach
    career tables, The Org scope paragraphs. Reddit is allowed here, where a hit
    can be checked against the fuller picture."""
    body = _post("/web/search", {
        "query": _query_for(name, company, title),
        "excludeDomains": _CALL3_EXCLUDE,
        "markdownOptions": {"enabled": True},
    }, timeout=_TIMEOUT_SCRAPE)
    bundle.credits_spent += _credits_used(body)

    already = set(bundle.fetched_urls)
    for r in _results(body):
        url = (r.get("url") or "").strip()
        # Absolute rule: never take what Calls 1 and 2 already own. Check the URL
        # AND the domain — excludeDomains is a request to the API, not a guarantee,
        # and a leaked x.com result would otherwise land in the web layer and
        # corrupt its source attribution.
        if url in already or any(d in url for d in _CALL3_EXCLUDE):
            continue
        if url:
            bundle.fetched_urls.append(url)
        md = _markdown(r)
        if md:
            bundle.web_markdown.append(md)

    logger.info("[Research] Call 3 (web): %d scraped docs", len(bundle.web_markdown))


# ── Public API ─────────────────────────────────────────────────────────────────

def research_lead(name: str, company: str, title: str = "") -> ResearchBundle:
    """Run up to three aimed calls. Returns whatever was gathered.

    Raises ContextDevAuthError on 401/402/403 so the caller can pause rather than
    burn retries. Other failures degrade: a bundle with partial signal is still
    useful, and a bundle with none yields a bare-ask email.
    """
    bundle = ResearchBundle()
    if not _enabled() or not name:
        return bundle

    max_credits = max(0, settings.CONTEXT_DEV_MAX_CREDITS_PER_LEAD)

    if bundle.credits_spent < max_credits:
        _call_linkedin(bundle, name, company)

    if bundle.credits_spent < max_credits:
        try:
            _call_x(bundle, name, company)
        except ContextDevAuthError:
            raise
        except ContextDevError as e:
            logger.warning("[Research] Call 2 failed for %s, continuing: %s", name, e)

    # A two-credit lead is a good lead.
    if bundle.credits_spent < max_credits and not bundle.is_rich():
        try:
            _call_web(bundle, name, company, title)
        except ContextDevAuthError:
            raise
        except ContextDevError as e:
            logger.warning("[Research] Call 3 failed for %s, continuing: %s", name, e)
    elif bundle.is_rich():
        logger.info("[Research] Skipped Call 3 for %s — Calls 1+2 sufficient", name)

    logger.info("[Research] %s: %d credits, signal=%s",
                name, bundle.credits_spent, bundle.has_signal())
    return bundle
