"""LLM-powered company research via Azure OpenAI Responses API + Bing web search.

Replaces Apollo /organizations/enrich. Given a company name (and optionally a
domain), performs a live Bing search and returns the same structured facts blob
used downstream by the justifier and lead scorer.

Why Responses API instead of /chat/completions:
  - Supports web_search_preview tool (live Bing results, no separate API key)
  - gpt-5-mini is a reasoning model — it synthesises web results accurately
  - Zero Apollo credits; Azure token cost is negligible on our plan

Key constraints discovered during testing:
  - JSON mode is INCOMPATIBLE with web_search_preview — we instruct the model
    to output JSON as plain text and parse it ourselves
  - gpt-5-mini uses ~2500 internal reasoning tokens per call; max_output_tokens
    must be ≥ 5000 to leave room for the actual JSON response
  - reasoning.effort "low" reduces latency from ~60s to ~15-25s per company
  - Timeout must be 120s (reasoning model is slow; 60s times out)
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional

import requests

from core.config import settings

logger = logging.getLogger(__name__)

_RESPONSES_URL = None  # built lazily from settings
_TIMEOUT_SEC = 120
_MAX_OUTPUT_TOKENS = 6000
_PARALLEL = 8  # concurrent company lookups

_PROMPT_TEMPLATE = """Search the web for information about the company "{company_name}"{domain_hint}.

Then return ONLY a raw JSON object (no markdown, no explanation) with exactly these fields:

{{
  "what_they_build": "One concrete sentence: what the product does and who it serves. Max 150 chars.",
  "core_tech": ["Up to 6 specific tech terms they actually use — e.g. LLM, RAG, PyTorch, Kubernetes"],
  "primary_market": "B2B/B2C + vertical — e.g. 'B2B SaaS for fintech' or 'B2C consumer mobile'. Max 60 chars.",
  "stage_signal": "One of: early-stage, growth, scale, mature",
  "recent_momentum": "One line about latest funding round, acquisition, or major news. null if nothing recent.",
  "hiring_signal": "Most relevant open engineering/product/AI role if they are hiring. null if not found.",
  "domain": "The company's official website domain e.g. waymo.com — no https, no www",
  "employee_count": 0,
  "industries": ["up to 3 industry labels"],
  "keywords": ["up to 6 keywords that describe the company"]
}}

Rules:
- CITE ONLY facts you found in the search results. Never invent.
- stage_signal: early-stage = <50 employees, growth = 50-500, scale = 500-5000, mature = 5000+
- If you cannot find reliable information for a field, use null (for strings) or [] (for arrays) or 0 (for numbers).
- Return ONLY the JSON object. No text before or after it.
"""


def _get_responses_url() -> str:
    global _RESPONSES_URL
    if _RESPONSES_URL is None:
        endpoint = settings.AZURE_OPENAI_ENDPOINT.rstrip("/")
        _RESPONSES_URL = f"{endpoint}/openai/responses?api-version={settings.AZURE_OPENAI_API_VERSION}"
    return _RESPONSES_URL


def _extract_json(text: str) -> Optional[dict]:
    """Extract and parse JSON from model output. Handles markdown fences."""
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # Find the outermost JSON object
    start = text.find("{")
    if start == -1:
        return None
    # Find the matching closing brace
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def research_company(name: str, domain: Optional[str] = None) -> Optional[dict]:
    """Research a single company via LLM web search.

    Returns a dict matching the extracted_facts schema, or None on failure.
    Never raises — failures are logged and swallowed so the pipeline continues.
    """
    if not name:
        return None
    if not settings.AZURE_OPENAI_KEY or not settings.AZURE_OPENAI_ENDPOINT:
        logger.warning("[LLM_RESEARCH] Azure OpenAI not configured, skipping")
        return None

    domain_hint = f" (their website may be {domain})" if domain else ""
    prompt = _PROMPT_TEMPLATE.format(company_name=name, domain_hint=domain_hint)

    payload = {
        "model": settings.AZURE_OPENAI_LLM_DEPLOYMENT,
        "input": prompt,
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
        "tools": [{"type": "web_search_preview"}],
        "reasoning": {"effort": "low"},
    }

    try:
        resp = requests.post(
            _get_responses_url(),
            headers={
                "api-key": settings.AZURE_OPENAI_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        logger.warning("[LLM_RESEARCH] HTTP error for %s: %s", name, e)
        return None

    if not resp.ok:
        logger.warning("[LLM_RESEARCH] API error %d for %s: %s", resp.status_code, name, resp.text[:300])
        return None

    data = resp.json()
    status = data.get("status")
    if status not in ("completed",):
        reason = (data.get("incomplete_details") or {}).get("reason", status)
        logger.warning("[LLM_RESEARCH] Incomplete response for %s: %s", name, reason)
        # Still try to parse partial output if present

    # Extract text from the message output block
    text_content = None
    for item in data.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    text_content = block.get("text", "")
                    break

    if not text_content:
        logger.warning("[LLM_RESEARCH] No text output for %s", name)
        return None

    parsed = _extract_json(text_content)
    if not parsed:
        logger.warning("[LLM_RESEARCH] Could not parse JSON for %s — raw: %s", name, text_content[:300])
        return None

    usage = data.get("usage", {})
    logger.info(
        "[LLM_RESEARCH] OK company=%s domain=%s tokens_in=%s tokens_out=%s",
        name, parsed.get("domain"), usage.get("input_tokens"), usage.get("output_tokens"),
    )
    return _normalise(parsed, name, domain)


def _normalise(raw: dict, name: str, fallback_domain: Optional[str]) -> dict:
    """Coerce raw LLM output into the canonical extracted_facts shape."""
    def _str(v, maxlen=200):
        if not v or not isinstance(v, str):
            return None
        return v.strip()[:maxlen] or None

    def _list(v, maxlen=8):
        if not v:
            return []
        if isinstance(v, str):
            return [v.strip()]
        return [str(x).strip() for x in v if x][:maxlen]

    def _int(v):
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    stage = _str(raw.get("stage_signal"))
    if stage not in ("early-stage", "growth", "scale", "mature"):
        stage = None

    discovered_domain = _str(raw.get("domain"), 100) or fallback_domain

    return {
        # Canonical extracted_facts fields (consumed by justifier + scorer)
        "what_they_build": _str(raw.get("what_they_build"), 200),
        "core_tech": _list(raw.get("core_tech"), 8),
        "primary_market": _str(raw.get("primary_market"), 80),
        "stage_signal": stage,
        "recent_momentum": _str(raw.get("recent_momentum"), 200),
        "hiring_signal": _str(raw.get("hiring_signal"), 200),
        # Company profile fields (persisted on CompanyProfile)
        "name": name,
        "discovered_domain": discovered_domain,
        "industries": _list(raw.get("industries"), 5),
        "keywords": _list(raw.get("keywords"), 10),
        "employee_count": _int(raw.get("employee_count")),
    }


def research_companies_bulk(
    companies: Dict[str, Optional[str]],
    parallel: int = _PARALLEL,
) -> Dict[str, Optional[dict]]:
    """Research multiple companies concurrently.

    Args:
        companies: {company_name: domain_or_None}
        parallel: max concurrent LLM calls

    Returns:
        {company_name: facts_dict_or_None}
    """
    if not companies:
        return {}

    results: Dict[str, Optional[dict]] = {name: None for name in companies}
    logger.info("[LLM_RESEARCH] bulk: %d companies, parallel=%d", len(companies), parallel)

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        future_to_name = {
            pool.submit(research_company, name, domain): name
            for name, domain in companies.items()
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results[name] = future.result()
            except Exception as e:
                logger.warning("[LLM_RESEARCH] worker exception for %s: %s", name, e)

    succeeded = sum(1 for v in results.values() if v is not None)
    logger.info("[LLM_RESEARCH] bulk done: %d/%d succeeded", succeeded, len(companies))
    return results
