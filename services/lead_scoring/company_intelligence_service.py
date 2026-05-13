"""Company Intelligence Engine — batched LLM evaluation of company fit.

After the full 500-lead collection (before scoring), evaluates each unique company
against the candidate's profile to produce a company_fit_score (1-10).

Batches 15 companies per LLM call to keep cost and latency reasonable.
Results are cached by company domain in CompanyProfile so repeat runs skip the call.

The company_fit_score is then attached to each lead and consumed by lead_scoring_service
as an additional scoring dimension (0-15 points normalized from the 1-10 LLM score).
"""

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_BATCH_SIZE = 15

_SYSTEM_PROMPT = """You are a recruiting quality analyst evaluating company fit for a specific candidate.

Given a candidate profile and a batch of companies (with name, industry, description, size),
rate each company's fit for this candidate on a scale of 1-10.

Scoring guidance:
  10 — Perfect fit: right industry, right stage, this type of company would actively want this candidate
  7-9 — Good fit: most signals align, minor mismatches
  4-6 — Neutral: plausible but not obvious fit
  2-3 — Poor fit: wrong sector or stage for this candidate
  1   — Clear mismatch: e.g. FMCG/alcohol/fashion for an AI/SaaS candidate wanting startups

For early-stage startup candidates: a small AI/SaaS/edtech company = high fit; a 10,000-person FMCG or real estate firm = very low fit.
For enterprise candidates: reverse applies.

Respond ONLY with valid JSON — no markdown, no prose:
{
  "evaluations": [
    {"company": "<exact company name>", "fit_score": <1-10>, "reason": "<one short phrase>"},
    ...
  ]
}"""


def _build_company_blurb(lead: dict) -> str:
    """Build a company description for LLM fit evaluation.

    Prefers structured extracted_facts (from LLM web search) — they're more
    specific and produce tighter fit scores than raw description strings.
    """
    facts = lead.get("extracted_facts") or {}
    name = lead.get("company") or "Unknown"

    if facts.get("what_they_build"):
        parts = [f"Company: {name}"]
        parts.append(f"What they build: {facts['what_they_build']}")
        if facts.get("primary_market"):
            parts.append(f"Market: {facts['primary_market']}")
        if facts.get("core_tech"):
            parts.append(f"Tech: {', '.join(facts['core_tech'][:5])}")
        if facts.get("stage_signal"):
            parts.append(f"Stage: {facts['stage_signal']}")
        if facts.get("recent_momentum"):
            parts.append(f"Momentum: {facts['recent_momentum']}")
        if facts.get("hiring_signal"):
            parts.append(f"Hiring: {facts['hiring_signal']}")
    else:
        parts = [
            f"Company: {name}",
            f"Industry: {lead.get('industry') or '?'}",
            f"Size: {lead.get('company_size') or '?'}",
        ]
        desc = (lead.get("company_description") or "").strip()
        if desc:
            parts.append(f"About: {desc[:200]}")

    return " | ".join(parts)


def _build_candidate_context(candidate_prefs: dict) -> str:
    stage = str(
        (candidate_prefs.get("company_stage") or ["any"])[0]
        if isinstance(candidate_prefs.get("company_stage"), list)
        else (candidate_prefs.get("company_stage") or "any")
    )
    niche = candidate_prefs.get("niche_keywords") or []
    roles = candidate_prefs.get("preferred_roles") or []
    archetype = (candidate_prefs.get("archetype_label") or "").strip()
    avoid = candidate_prefs.get("company_type_avoid") or []
    return (
        f"Company stage preference: {stage}\n"
        f"Profile archetype: {archetype or 'not specified'}\n"
        f"Target roles: {', '.join(roles[:4]) if roles else 'not specified'}\n"
        f"Niche focus: {', '.join(niche[:4]) if niche else 'none'}\n"
        f"Company types to avoid: {', '.join(avoid[:3]) if avoid else 'none'}"
    )


def _call_llm_batch(companies: list[dict], candidate_context: str) -> dict[str, int]:
    """Call LLM with a batch of companies. Returns {company_name_lower: fit_score}."""
    from openai import AzureOpenAI
    from core.config import settings

    company_list = "\n".join(
        f"{i + 1}. {_build_company_blurb(c)}"
        for i, c in enumerate(companies)
    )
    user_content = (
        f"CANDIDATE PROFILE:\n{candidate_context}\n\n"
        f"COMPANIES TO EVALUATE ({len(companies)}):\n{company_list}\n\n"
        "Rate each company's fit for this candidate."
    )

    client = AzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )
    response = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=600,
    )
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:].strip()

    result = json.loads(raw)
    scores: dict[str, int] = {}
    for item in result.get("evaluations") or []:
        name = (item.get("company") or "").lower().strip()
        score = int(item.get("fit_score") or 5)
        score = max(1, min(10, score))
        if name:
            scores[name] = score
            logger.debug("[CompanyIntel] %s → %d/10 (%s)", name, score, item.get("reason", ""))
    return scores


def evaluate_company_fit(
    leads: list[dict],
    candidate_prefs: dict,
    db: Session,
) -> dict[str, int]:
    """Evaluate company fit for all unique companies in the lead batch.

    Groups leads by company name, batches 15 companies per LLM call.
    Checks the CompanyProfile cache (company_fit_score field) before calling LLM.

    Args:
        leads: List of lead dicts (must have 'company', 'company_domain', 'industry',
               'company_size', 'company_description').
        candidate_prefs: Same format as quality_probe_loop candidate_prefs.
        db: SQLAlchemy session.

    Returns:
        Dict mapping company_name_lower → fit_score (1-10).
        Missing entries default to 5 (neutral) in the caller.
    """
    from database.models import CompanyProfile

    # Deduplicate by company name (lowercased)
    unique: dict[str, dict] = {}
    for lead in leads:
        name = (lead.get("company") or "").strip()
        if name and name.lower() not in unique:
            unique[name.lower()] = lead

    if not unique:
        return {}

    candidate_context = _build_candidate_context(candidate_prefs)
    company_fit_scores: dict[str, int] = {}
    to_evaluate: list[dict] = []

    # Check cache: CompanyProfile by domain first, then by name.
    # Also inject extracted_facts into the lead dict so _build_company_blurb
    # produces richer blurbs when LLM web search data is available.
    for name_lower, lead in unique.items():
        domain = lead.get("company_domain") or ""
        cached_score = None
        cp = None

        if domain:
            cp = db.query(CompanyProfile).filter(CompanyProfile.domain == domain).first()
        if cp is None:
            cp = db.query(CompanyProfile).filter(CompanyProfile.name.ilike(name_lower)).first()

        if cp:
            if getattr(cp, "company_fit_score", None) is not None:
                cached_score = cp.company_fit_score
            # Inject extracted_facts so blurb is richer even on a score cache miss
            if cp.extracted_facts and not lead.get("extracted_facts"):
                lead["extracted_facts"] = cp.extracted_facts

        if cached_score is not None:
            company_fit_scores[name_lower] = int(cached_score)
            logger.debug("[CompanyIntel] Cache hit for %s: %d/10", name_lower, cached_score)
        else:
            to_evaluate.append(lead)

    logger.info(
        "[CompanyIntel] %d unique companies: %d cached, %d to evaluate via LLM",
        len(unique), len(company_fit_scores), len(to_evaluate),
    )

    # Batch LLM calls
    for i in range(0, len(to_evaluate), _BATCH_SIZE):
        batch = to_evaluate[i: i + _BATCH_SIZE]
        try:
            batch_scores = _call_llm_batch(batch, candidate_context)
            company_fit_scores.update(batch_scores)
            logger.info(
                "[CompanyIntel] Batch %d/%d evaluated %d companies",
                i // _BATCH_SIZE + 1,
                (len(to_evaluate) + _BATCH_SIZE - 1) // _BATCH_SIZE,
                len(batch_scores),
            )
        except Exception as exc:
            logger.warning("[CompanyIntel] LLM batch failed (%s) — defaulting batch to 5", exc)
            for lead in batch:
                name_lower = (lead.get("company") or "").lower().strip()
                if name_lower:
                    company_fit_scores.setdefault(name_lower, 5)

    return company_fit_scores
