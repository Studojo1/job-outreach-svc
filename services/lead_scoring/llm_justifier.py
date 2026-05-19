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

BATCH_SIZE = 12
PARALLEL_BATCHES = 10
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

_SYSTEM_PROMPT = """You write per-lead outreach reasoning for a job-search product. Each entry is a compact flashcard: one headline + exactly 3 bullets explaining why THIS candidate should contact THIS lead.

INPUT: structured company facts (what_they_build, core_tech, recent_momentum, hiring_signal) — use these first. Candidate profile (subdomain, top_skills, tech_stack, target_industries, flex project). Lead title/name/location. Raw company text as fallback only.

RULES:
1. Real facts only — cite what_they_build, core_tech, hiring_signal, or candidate skills/project verbatim. Never invent.
2. One candidate-signal × one company-signal per bullet. Strongest link first, then 2nd, then soft link (location/size/market).
3. Headline ≤80 chars, each bullet ≤80 chars. No filler.
4. FORBIDDEN phrases: "strong fit", "great match", "good fit", "perfect alignment", "reach out promptly", "ideal candidate", "perfect match", "excellent opportunity", "amazing", "exciting", any generic "X aligns with your Y". No em dashes (—) — use hyphen (-).
5. signal_strength: "high" = direct tech/niche/project overlap; "medium" = sensible role/location fit; "low" = title-only guess. If data is thin, mark "low".

Output a single JSON object keyed by lead_id (string) covering ALL leads in this batch."""

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
        facts = company.get("facts") or {}
        rich_facts = sum(1 for k in ("what_they_build", "core_tech", "recent_momentum", "hiring_signal") if facts.get(k))

        if facts:
            if facts.get("what_they_build"):
                lines.append(f"  builds: {facts['what_they_build']}")
            if facts.get("core_tech"):
                lines.append(f"  tech: {', '.join(facts['core_tech'])}")
            if facts.get("primary_market"):
                lines.append(f"  market: {facts['primary_market']}")
            if facts.get("stage_signal"):
                lines.append(f"  stage: {facts['stage_signal']}")
            if facts.get("recent_momentum"):
                lines.append(f"  momentum: {facts['recent_momentum']}")
            if facts.get("hiring_signal"):
                lines.append(f"  hiring: {facts['hiring_signal']}")

        # Only include raw fallback fields when structured facts are sparse (<2 rich fields).
        # When facts are rich, raw text is redundant and wastes tokens.
        if rich_facts < 2:
            if company.get("short_description"):
                lines.append(f"  desc: {company['short_description']}")
            if company.get("industries"):
                lines.append(f"  industries: {', '.join(company['industries'])}")
            if company.get("keywords"):
                lines.append(f"  keywords: {', '.join(company['keywords'][:8])}")
            if company.get("technologies"):
                lines.append(f"  tech_stack: {', '.join(company['technologies'][:8])}")
            if company.get("website_summary"):
                lines.append(f"  website: {company['website_summary'][:200]}")

        # Always include headcount and hiring signals — useful regardless of fact richness
        if company.get("employee_count"):
            lines.append(f"  headcount: {company['employee_count']}")
        if company.get("headquarters_city"):
            lines.append(f"  hq: {company['headquarters_city']}")
        if company.get("recent_job_postings"):
            posts = ", ".join(p.get("title", "") for p in company["recent_job_postings"][:2] if p.get("title"))
            if posts:
                lines.append(f"  open_roles: {posts}")
    elif lead.get("company_description"):
        lines.append(f"  desc: {lead['company_description'][:200]}")
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

=== LEADS ===
{chr(10).join(lead_blocks)}

Return a JSON object keyed by lead_id (string). Each value: headline (≤80 chars, name the company), bullets (3 items ≤80 chars each), signal_strength (high/medium/low).
"""


def _build_company_snapshot(lead: dict, company: Optional[dict]) -> str:
    """Build a deterministic one-liner: what they build · city · stage or headcount."""
    parts = []
    if company:
        facts = company.get("facts") or {}
        what = facts.get("what_they_build") or company.get("short_description") or ""
        if what:
            parts.append(what[:80])
        city = facts.get("headquarters_city") or company.get("headquarters_city") or lead.get("location") or ""
        if city:
            parts.append(city)
        stage = facts.get("stage_signal") or ""
        headcount = company.get("employee_count") or ""
        size_label = stage or (f"~{headcount} employees" if headcount else "")
        if size_label:
            parts.append(size_label)
    else:
        company_name = lead.get("company") or ""
        location = lead.get("location") or ""
        if company_name:
            parts.append(company_name)
        if location:
            parts.append(location)
    return " · ".join(parts) if parts else ""


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
        item = _strip_em_dashes(item)
        snapshot = _build_company_snapshot(lead, _resolve_company(lead, companies))
        if snapshot and len(item.get("bullets") or []) >= 2:
            item["bullets"] = [snapshot] + item["bullets"][:2]
        out[lead["id"]] = item
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
