"""Per-company fact extractor (LLM).

Turns the raw mess of company data we collect (Apollo description, keywords,
industries, news, multi-page scrape sections, job postings) into a small,
structured facts blob that the justifier and the scorer can both reason about.

Why this exists: the justifier was being handed a flat text dump and asked to
"find the most concrete link", which it tried but mostly produced surface-level
bullets like "Company focus on AI aligns with your AI expertise". A focused
extractor LLM with a tight schema produces facts the justifier can cite.

Output schema (per company):
    {
      "what_they_build": "1-line concrete description",
      "core_tech": ["NLP", "ASR", "PyTorch", ...],          # up to 8
      "primary_market": "B2B SaaS for fintech" | "consumer mobile" | etc.,
      "stage_signal": "early-stage" | "growth" | "scale" | "mature",
      "recent_momentum": "raised Series B Nov 2025" | null,
      "hiring_signal": "LLM Engineer with PyTorch" | null
    }

Cost / latency: batched 5 companies per call, parallel pool of 5 batches.
80 unique companies → 16 batches → ~3 in-flight LLM calls at any time.
~$0.05/run total, ~25s wall time.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from services.shared.ai.azure_openai_client import generate_json

logger = logging.getLogger(__name__)

BATCH_SIZE = 5
PARALLEL_BATCHES = 5
TEMPERATURE = 0.2

# Phrases the extractor LLM is forbidden from emitting in `what_they_build`
# (they're indistinguishable from each other and useless to the justifier).
_FLUFF_DENYLIST = [
    "innovative",
    "leading provider",
    "cutting-edge",
    "state-of-the-art",
    "world-class",
    "next-generation",
    "industry-leading",
    "trusted partner",
    "transformative",
    "synergies",
]

_SYSTEM_PROMPT = """You are a company fact extractor for a job-outreach product.
For each company in the batch, distill the provided raw data into a small,
verifiable facts blob the downstream justifier will cite verbatim.

Rules:

1. CITE ONLY WHAT'S IN THE DATA. Never invent. If a field can't be supported
   by the provided text, return null.

2. BE SPECIFIC. "what_they_build" must be a single concrete sentence —
   what their product DOES and WHO it's FOR. Examples:
   - "in-store voice analytics for retail chains"
   - "LLM-based call summarization and CRM auto-update for sales teams"
   - "BNPL credit-card alternative for US consumers"
   Bad: "innovative AI platform", "leading SaaS solution".

3. CORE_TECH = the technical stack/methods they actually use, drawn from
   tech keywords, blog posts, careers postings, or job descriptions. Up to 8 items.
   Examples: ["LLM", "RAG", "PyTorch", "ASR", "transformers", "Kubernetes"].
   Don't include generic words like "AI", "SaaS", "platform" — they don't differentiate.

4. PRIMARY_MARKET = "{B2B|B2C|B2G|internal} {industry vertical}".
   Examples: "B2B SaaS for fintech", "B2C mobile retail", "B2B HRtech",
   "internal IT for industrials". This is what gets compared against the
   candidate's target_industries downstream.

5. STAGE_SIGNAL = one of: "early-stage" (<50 emp), "growth" (50-500 emp),
   "scale" (500-5000), "mature" (5000+). Use the headcount field; null if unknown.

6. RECENT_MOMENTUM = one short line if there's a recent funding round,
   product launch, or news event in the data. Otherwise null.

7. HIRING_SIGNAL = if job_postings is provided AND has at least one posting
   matching engineering / product / design / data / ML / AI roles, return
   the most relevant title + one-line description. Otherwise null.

8. ABSOLUTELY FORBIDDEN words in any field:
   "innovative", "leading provider", "cutting-edge", "state-of-the-art",
   "world-class", "next-generation", "industry-leading", "trusted partner",
   "transformative", "synergies".

Return ONE JSON object whose top-level keys are the company domains (strings),
each mapping to a fact-blob matching the schema above.
"""

_FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "what_they_build": {"type": ["string", "null"], "maxLength": 200},
        "core_tech": {
            "type": "array",
            "items": {"type": "string", "maxLength": 40},
            "maxItems": 8,
        },
        "primary_market": {"type": ["string", "null"], "maxLength": 80},
        "stage_signal": {
            "type": ["string", "null"],
            "enum": ["early-stage", "growth", "scale", "mature", None],
        },
        "recent_momentum": {"type": ["string", "null"], "maxLength": 200},
        "hiring_signal": {"type": ["string", "null"], "maxLength": 200},
    },
    "required": [
        "what_they_build", "core_tech", "primary_market",
        "stage_signal", "recent_momentum", "hiring_signal",
    ],
    "additionalProperties": False,
}


def _build_batch_schema(domains: List[str]) -> dict:
    return {
        "type": "object",
        "properties": {d: _FACTS_SCHEMA for d in domains},
        "required": list(domains),
        "additionalProperties": False,
    }


def _format_company_block(domain: str, profile_dict: dict) -> str:
    """Render a single company's raw enrichment data as a flat text block
    the LLM extractor can read. profile_dict is the result of
    profile_to_extractor_dict() in company_enrichment_service."""
    lines = [f"=== {domain} ==="]
    if profile_dict.get("name"):
        lines.append(f"name: {profile_dict['name']}")
    if profile_dict.get("short_description"):
        lines.append(f"apollo_description: {profile_dict['short_description']}")
    if profile_dict.get("industries"):
        lines.append(f"industries: {', '.join(profile_dict['industries'])}")
    if profile_dict.get("keywords"):
        lines.append(f"keywords: {', '.join(profile_dict['keywords'])}")
    if profile_dict.get("technologies"):
        lines.append(f"technologies: {', '.join(profile_dict['technologies'])}")
    if profile_dict.get("employee_count") is not None:
        lines.append(f"headcount: {profile_dict['employee_count']}")
    if profile_dict.get("founded_year"):
        lines.append(f"founded_year: {profile_dict['founded_year']}")
    if profile_dict.get("headquarters_city"):
        lines.append(f"hq: {profile_dict['headquarters_city']}")
    if profile_dict.get("recent_news_summary"):
        lines.append(f"recent_news: {profile_dict['recent_news_summary']}")
    if profile_dict.get("latest_funding_amount"):
        lines.append(f"funding: ${profile_dict['latest_funding_amount']:,}")
    if profile_dict.get("website_summary"):
        lines.append(f"homepage_about: {profile_dict['website_summary']}")
    if profile_dict.get("product_page_summary"):
        lines.append(f"product_pages: {profile_dict['product_page_summary']}")
    if profile_dict.get("blog_summary"):
        lines.append(f"blog: {profile_dict['blog_summary']}")
    if profile_dict.get("careers_summary"):
        lines.append(f"careers_page: {profile_dict['careers_summary']}")
    postings = profile_dict.get("recent_job_postings") or []
    if postings:
        lines.append("job_postings:")
        for jp in postings[:5]:
            line = f"  - {jp.get('title')}"
            if jp.get("snippet"):
                line += f" — {jp['snippet']}"
            lines.append(line)
    return "\n".join(lines)


def _build_batch_prompt(batch: Dict[str, dict]) -> str:
    blocks = "\n\n".join(_format_company_block(d, p) for d, p in batch.items())
    domains_list = ", ".join(batch.keys())
    return f"""{_SYSTEM_PROMPT}

Companies in this batch (extract facts for each):
{blocks}

Return a JSON object with these top-level keys: {domains_list}.
Each value must match the fact schema (what_they_build, core_tech, primary_market,
stage_signal, recent_momentum, hiring_signal). Use null for unknown fields.
"""


def _strip_fluff(facts: dict) -> dict:
    """Remove or null-out fields that contain fluff phrases."""
    cleaned = dict(facts)
    for key in ("what_they_build", "primary_market", "recent_momentum", "hiring_signal"):
        val = cleaned.get(key)
        if isinstance(val, str) and any(f in val.lower() for f in _FLUFF_DENYLIST):
            cleaned[key] = None
    # Filter generic tech terms from core_tech
    tech = cleaned.get("core_tech") or []
    cleaned["core_tech"] = [
        t for t in tech
        if isinstance(t, str)
        and t.lower() not in {"ai", "ml", "saas", "platform", "software", "technology", "cloud"}
    ]
    return cleaned


def _extract_batch(batch: Dict[str, dict]) -> Dict[str, dict]:
    if not batch:
        return {}
    prompt = _build_batch_prompt(batch)
    schema = _build_batch_schema(list(batch.keys()))
    try:
        result = generate_json(prompt, schema, temperature=TEMPERATURE)
    except Exception as e:
        logger.warning("[FACTS] batch failed (%d companies): %s", len(batch), e)
        return {}

    out: Dict[str, dict] = {}
    for d in batch:
        if d not in result:
            continue
        out[d] = _strip_fluff(result[d])
    return out


def extract_company_facts(
    companies: Dict[str, dict],
    batch_size: int = BATCH_SIZE,
    parallel_batches: int = PARALLEL_BATCHES,
) -> Dict[str, Optional[dict]]:
    """Extract structured facts for every company in `companies`.

    Args:
        companies: {domain: profile_dict_for_extractor}
        batch_size: companies per LLM call.
        parallel_batches: concurrent in-flight batches.

    Returns:
        {domain: facts_dict_or_None}. Domains where extraction failed get None.
    """
    if not companies:
        return {}

    domains = list(companies.keys())
    batches = [
        {d: companies[d] for d in domains[i:i + batch_size]}
        for i in range(0, len(domains), batch_size)
    ]
    logger.info(
        "[FACTS] starting — %d companies, %d batches of %d (parallel=%d)",
        len(domains), len(batches), batch_size, parallel_batches,
    )

    out: Dict[str, Optional[dict]] = {d: None for d in domains}
    with ThreadPoolExecutor(max_workers=parallel_batches) as pool:
        futures = [pool.submit(_extract_batch, b) for b in batches]
        for fut in as_completed(futures):
            try:
                for d, facts in fut.result().items():
                    out[d] = facts
            except Exception as e:
                logger.warning("[FACTS] batch worker exception: %s", e)

    succeeded = sum(1 for v in out.values() if v is not None)
    logger.info("[FACTS] complete — %d/%d companies extracted", succeeded, len(domains))
    return out
