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

BATCH_SIZE = 5
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

Rules:

1. CITE REAL FACTS ONLY. Every bullet must reference something concrete from the company data
   (industry, product, tech stack, headcount, HQ city) OR the lead (title, seniority).
   Never invent facts not in the provided data.

2. ONE CANDIDATE-COMPANY LINK PER BULLET. Each bullet should connect a candidate signal
   (target role, niche, tech stack, flex project) to a company signal. Examples:
   - "Builds client-acquisition SaaS — same problem space as your AI-agent project"
   - "Stack overlap: Python + Kubernetes (your daily tools)"
   - "Hiring ML engineers actively in Bengaluru (your city)"

3. CONCISE. Each bullet ≤ 80 chars. Headline ≤ 80 chars. No throat-clearing.

4. NO FLUFF PHRASES. Forbidden: "strong fit", "great match", "good fit", "perfect alignment",
   "reach out promptly", "ideal candidate", "perfect match", "excellent opportunity",
   "great opportunity", "amazing", "exciting".

5. DEGRADE GRACEFULLY. If company data is thin, anchor bullets on title/seniority/location/size.

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
    return {
        "type": "object",
        "properties": {str(lid): _LEAD_SCHEMA for lid in lead_ids},
        "required": [str(lid) for lid in lead_ids],
        "additionalProperties": False,
    }


def _format_candidate_block(candidate: dict) -> str:
    flex = candidate.get("flex_notes") or {}
    parts = [
        f"Name: {candidate.get('name') or 'unspecified'}",
        f"Target roles: {', '.join(candidate.get('target_roles') or []) or 'unspecified'}",
        f"Niche keywords: {', '.join(candidate.get('niche_keywords') or []) or 'none'}",
        f"Tech stack: {', '.join(candidate.get('tech_stack') or []) or 'none'}",
        f"Career stage: {candidate.get('career_stage') or 'unspecified'}",
        f"Locations: {', '.join(candidate.get('locations') or []) or 'flexible'}",
    ]
    if flex.get("best_project"):
        parts.append(f"Flex project they built: {flex['best_project']}")
    if flex.get("outcome"):
        parts.append(f"Outcome / impact they cite: {flex['outcome']}")
    return "\n".join(parts)


def _format_lead_block(lead: dict, company: Optional[dict]) -> str:
    lines = [
        f"lead_id: {lead['id']}",
        f"  Name: {lead.get('name') or 'unknown'}",
        f"  Title: {lead.get('title') or 'unknown'}",
        f"  Company: {lead.get('company') or 'unknown'}",
        f"  Location: {lead.get('location') or 'unknown'}",
    ]
    if company:
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
        if company.get("website_summary"):
            lines.append(f"  Website summary: {company['website_summary']}")
    elif lead.get("company_description"):
        lines.append(f"  Company description (Apollo only): {lead['company_description']}")
    return "\n".join(lines)


def _build_batch_prompt(candidate: dict, leads: List[dict], companies: Dict[str, dict]) -> str:
    candidate_block = _format_candidate_block(candidate)
    lead_blocks = []
    for lead in leads:
        domain = (lead.get("company_domain") or "").lower()
        company_payload = companies.get(domain)
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
        out[lead["id"]] = item
    return out


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
