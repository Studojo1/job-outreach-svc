"""LLM-based per-lead justification.

After heuristic scoring picks the top-K leads, this service generates a
specific, grounded reason-to-reach-out for each one. Output is structured
JSON so the frontend can render the parts independently (headline, body,
talk-track) and color-code by signal_strength.

Batched 5 leads per LLM call to amortize overhead. Failures fall back to
None per-lead — the frontend then uses its existing heuristic template.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from services.shared.ai.azure_openai_client import generate_json

logger = logging.getLogger(__name__)

BATCH_SIZE = 4
PARALLEL_BATCHES = 5
TEMPERATURE = 0.4

# Phrases the LLM is forbidden from using — these are the markers of
# the old client-side heuristic template ("strong fit", "reach out promptly",
# "great match", etc.). If we don't ban them, the LLM falls back to them
# whenever the company data is thin.
BANNED_PHRASES = [
    "strong fit",
    "great match",
    "perfect alignment",
    "great fit",
    "good fit",
    "strong match",
    "decision-maker — strong fit",
    "reach out promptly",
    "ideal candidate",
    "perfect match",
    "excellent opportunity",
]

_SYSTEM_PROMPT = """You are writing per-lead reasoning for a job-outreach product.
Each flashcard explains why ONE specific candidate should reach out to ONE specific lead at
ONE specific company. Output is rendered in a compact card — content MUST be tight.

Output shape: a one-line headline + exactly 3 short bullets.

You are given:
- Structured facts per company (what_they_build, core_tech, primary_market,
  recent_momentum, hiring_signal). PRIORITIZE these — they're already distilled.
- A wider candidate profile including their actual specialization (subdomain),
  top skills, target industries, target roles, tech stack, and a flex project.
- The lead's title, name, location, plus raw company description and website
  summary as fallback.

Rules:

1. CITE REAL FACTS ONLY. Every bullet must reference something concrete from the
   company facts OR the lead. Never invent.

2. PREFER STRUCTURED FACTS. When `what_they_build`, `core_tech`,
   `recent_momentum`, or `hiring_signal` is provided, use it verbatim or close to it.
   Examples of GOOD bullets:
   - "Builds LLM/RAG to summarize sales calls — exact match to your ASR background"
   - "Stack overlap: PyTorch + transformers (your core tools)"
   - "Hiring an LLM Engineer right now — your role"
   - "Just raised Series B — growth-stage hiring push"

3. ONE CANDIDATE-COMPANY LINK PER BULLET. Connect a candidate signal
   (subdomain, top_skills, tech_stack, target_industries, flex project) to a
   company signal (what_they_build, core_tech, recent_momentum, hiring_signal).
   Bullet 1 = strongest link. Bullet 2 = second strongest. Bullet 3 = the
   "soft" link (location, headcount, market overlap).

4. CONCISE. Each bullet ≤ 80 chars. Headline ≤ 80 chars. No throat-clearing.

5. ABSOLUTELY FORBIDDEN. Never use these surface-level phrases:
   "strong fit", "great match", "good fit", "perfect alignment", "reach out promptly",
   "ideal candidate", "perfect match", "excellent opportunity", "great opportunity",
   "amazing", "exciting", "company focus on AI aligns with your AI expertise"
   (or any generic "X aligns with your Y" without naming X and Y specifically),
   "shared Python expertise" (without naming what for).
   Also NEVER use em dashes (—) anywhere. Use a plain hyphen (-) or restructure the sentence.

6. DEGRADE GRACEFULLY. If structured facts are missing AND raw text is thin,
   anchor on title, seniority, location overlap. Mark signal_strength: "low".

Output one JSON object containing reasoning for ALL leads in this batch, keyed by lead_id (string)."""

_LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "minLength": 10, "maxLength": 90},
        "bullets": {
            "type": "array",
            "items": {"type": "string", "minLength": 8, "maxLength": 90},
            "minItems": 3,
            "maxItems": 3,
        },
        "signal_strength": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["headline", "bullets", "signal_strength"],
    "additionalProperties": False,
}


def _build_batch_schema(lead_ids: List[int]) -> dict:
    # NOTE: we do NOT mark per-lead keys as required. If the LLM omits one
    # we accept the partial result (caller drops missing leads gracefully).
    # Previously we required every lead, which caused the whole batch to
    # retry 3x (~90s wasted) on a single missed lead. The retries don't help
    # because the LLM keeps hitting the same context limit.
    return {
        "type": "object",
        "properties": {str(lid): _LEAD_SCHEMA for lid in lead_ids},
        "additionalProperties": False,
    }


def _format_candidate_block(candidate: dict) -> str:
    flex = candidate.get("flex_notes") or {}
    parts = [
        f"Name: {candidate.get('name') or 'unspecified'}",
        f"Target roles: {', '.join(candidate.get('target_roles') or []) or 'unspecified'}",
        # Round-2: surface the LLM-extracted specialization so the justifier
        # can write subdomain-specific bullets (e.g. "ASR background", not
        # generic "ML background").
        f"Actual specialization (subdomain): {candidate.get('subdomain') or 'unspecified'}",
        f"Top skills (from resume): {', '.join(candidate.get('top_skills') or []) or 'none'}",
        f"Target industries: {', '.join(candidate.get('target_industries') or []) or 'none'}",
        f"Niche keywords (from quiz): {', '.join(candidate.get('niche_keywords') or []) or 'none'}",
        f"Tech stack: {', '.join(candidate.get('tech_stack') or []) or 'none'}",
        f"Career stage: {candidate.get('career_stage') or 'unspecified'}",
        f"Locations: {', '.join(candidate.get('locations') or []) or 'flexible'}",
    ]
    if flex.get("best_project"):
        parts.append(f"Flex project they built: {flex['best_project']}")
    if flex.get("outcome"):
        parts.append(f"Outcome / impact they cite: {flex['outcome']}")
    return "\n".join(parts)


def _resolve_company(lead: dict, companies: Dict[str, dict]) -> Optional[dict]:
    """Look up company profile by domain first, then by company name."""
    domain = (lead.get("company_domain") or "").lower()
    if domain:
        hit = companies.get(domain)
        if hit:
            return hit
    name = (lead.get("company") or "").strip()
    if name:
        hit = companies.get(name)
        if hit:
            return hit
        # Try case-insensitive scan as last resort
        name_lower = name.lower()
        for k, v in companies.items():
            if k.lower() == name_lower:
                return v
    return None


def _format_lead_block(lead: dict, company: Optional[dict]) -> str:
    lines = [
        f"lead_id: {lead['id']}",
        f"  Name: {lead.get('name') or 'unknown'}",
        f"  Title: {lead.get('title') or 'unknown'}",
        f"  Company: {lead.get('company') or 'unknown'}",
        f"  Location: {lead.get('location') or 'unknown'}",
    ]
    if company:
        # Round-2: prioritize the structured facts blob produced by the
        # company_fact_extractor LLM. The justifier should anchor bullets on
        # these high-signal fields first.
        facts = company.get("facts") or {}
        if facts:
            lines.append("  --- Structured facts (USE THESE FIRST) ---")
            if facts.get("what_they_build"):
                lines.append(f"  what_they_build: {facts['what_they_build']}")
            if facts.get("core_tech"):
                lines.append(f"  core_tech: {', '.join(facts['core_tech'])}")
            if facts.get("primary_market"):
                lines.append(f"  primary_market: {facts['primary_market']}")
            if facts.get("stage_signal"):
                lines.append(f"  stage: {facts['stage_signal']}")
            if facts.get("recent_momentum"):
                lines.append(f"  recent_momentum: {facts['recent_momentum']}")
            if facts.get("hiring_signal"):
                lines.append(f"  hiring_signal: {facts['hiring_signal']}")
            lines.append("  --- Raw fallback (use only if facts are thin) ---")
        if company.get("short_description"):
            lines.append(f"  Company description: {company['short_description']}")
        if company.get("industries"):
            lines.append(f"  Company industries: {', '.join(company['industries'])}")
        if company.get("keywords"):
            lines.append(f"  Company keywords: {', '.join(company['keywords'])}")
        if company.get("technologies"):
            lines.append(f"  Company tech: {', '.join(company['technologies'])}")
        if company.get("employee_count"):
            lines.append(f"  Headcount: {company['employee_count']}")
        if company.get("headquarters_city"):
            lines.append(f"  HQ: {company['headquarters_city']}")
        if company.get("recent_news_summary"):
            lines.append(f"  Recent news: {company['recent_news_summary']}")
        if company.get("recent_job_postings"):
            posts = ", ".join(p.get("title", "") for p in company["recent_job_postings"][:3] if p.get("title"))
            if posts:
                lines.append(f"  Active job postings: {posts}")
        if company.get("website_summary"):
            lines.append(f"  Website summary: {company['website_summary']}")
    elif lead.get("company_description"):
        lines.append(f"  Company description (Apollo only): {lead['company_description']}")
    return "\n".join(lines)


def _build_batch_prompt(candidate: dict, leads: List[dict], companies: Dict[str, dict]) -> str:
    candidate_block = _format_candidate_block(candidate)
    lead_blocks = []
    for lead in leads:
        company_payload = _resolve_company(lead, companies)
        lead_blocks.append(_format_lead_block(lead, company_payload))

    return f"""{_SYSTEM_PROMPT}

=== CANDIDATE ===
{candidate_block}

=== LEADS IN THIS BATCH ===
{chr(10).join(lead_blocks)}

For EACH lead above, produce a JSON object with these fields:
- headline: one line (≤80 chars). Should mention the company by name. Concrete, no fluff.
- bullets: EXACTLY 3 items, each ≤80 chars. Each bullet ties one candidate signal to one
  company signal. Lead with the strongest link (niche or tech overlap), then 2nd-strongest, etc.
  Examples:
    "Builds client-acquisition SaaS — same space as your AI-agent project"
    "Stack overlap: Python + Kubernetes (your daily tools)"
    "Hiring ML engineers actively in Bengaluru (your city)"
- signal_strength: "high" for direct concrete overlap (tech / niche / project topic match);
  "medium" for sensible role/seniority/location fit; "low" for generic title-based fit only.

Return a JSON object whose top-level keys are the lead_ids (as strings).
"""


def _justify_batch(candidate: dict, batch_leads: List[dict], companies: Dict[str, dict]) -> Dict[int, dict]:
    if not batch_leads:
        return {}
    prompt = _build_batch_prompt(candidate, batch_leads, companies)
    schema = _build_batch_schema([l["id"] for l in batch_leads])
    try:
        result = generate_json(prompt, schema, temperature=TEMPERATURE)
    except Exception as e:
        logger.warning("[JUSTIFY] batch failed (%d leads): %s", len(batch_leads), e)
        return {}

    out: Dict[int, dict] = {}
    for lead in batch_leads:
        lid = str(lead["id"])
        if lid not in result:
            continue
        item = result[lid]
        if _has_banned_phrase(item):
            logger.info("[JUSTIFY] dropped lead %s — banned phrase in output", lid)
            continue
        out[lead["id"]] = _strip_em_dashes(item)
    return out


def _strip_em_dashes(item: dict) -> dict:
    item["headline"] = item["headline"].replace("—", "-")
    item["bullets"] = [b.replace("—", "-") for b in item["bullets"]]
    return item


def _has_banned_phrase(item: dict) -> bool:
    parts = [str(item.get("headline") or "")]
    parts.extend(str(b) for b in (item.get("bullets") or []))
    blob = " ".join(parts).lower()
    return any(b in blob for b in BANNED_PHRASES)


def justify_leads(
    leads_with_scores: List[dict],
    candidate_context: dict,
    company_profiles: Dict[str, dict],
    batch_size: int = BATCH_SIZE,
    parallel_batches: int = PARALLEL_BATCHES,
) -> Dict[int, dict]:
    """Generate per-lead justifications for the input leads.

    Args:
        leads_with_scores: list of dicts; each must have id, name, title, company,
            company_domain, location, company_description.
        candidate_context: dict with name, target_roles, niche_keywords, tech_stack,
            career_stage, locations, flex_notes ({best_project, outcome}).
        company_profiles: {domain: profile_dict} (output of profile_to_llm_dict).
        batch_size: leads per LLM call.
        parallel_batches: concurrent LLM calls.

    Returns:
        {lead_id: {headline, fit_reason, talk_track, signal_strength}}
        Leads that failed get no entry — caller should treat absence as "use fallback".
    """
    if not leads_with_scores:
        return {}

    batches = [leads_with_scores[i:i + batch_size] for i in range(0, len(leads_with_scores), batch_size)]
    logger.info("[JUSTIFY] starting — %d leads, %d batches of %d (parallel=%d)",
                len(leads_with_scores), len(batches), batch_size, parallel_batches)

    results: Dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=parallel_batches) as pool:
        futures = [pool.submit(_justify_batch, candidate_context, batch, company_profiles)
                   for batch in batches]
        for fut in as_completed(futures):
            try:
                results.update(fut.result())
            except Exception as e:
                logger.warning("[JUSTIFY] batch worker exception: %s", e)

    logger.info("[JUSTIFY] complete — %d/%d leads got justification",
                len(results), len(leads_with_scores))
    return results
