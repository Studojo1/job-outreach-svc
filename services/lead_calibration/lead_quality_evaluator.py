"""LLM-based lead quality evaluator for the pre-collection probe loop.

Given a sample of probe leads and the candidate's full profile, calls Azure OpenAI
to assess quality and return specific filter adjustments in JSON.

Used by quality_probe_loop() in lead_collector_service.py before the full 500-lead
collection runs. Allows the system to self-correct filters based on qualitative lead
assessment rather than just a count-based loosening strategy.

Schema (v2 — May 2026):
  quality_score            — 1-10 overall batch quality
  main_issue               — top problem in one sentence (or null)
  company_evaluations      — per-company scores and reasons (up to 20)
  explicit_exclusions      — company names to block from full collection
  restrict_company_sizes   — Apollo size ranges to restrict to
  add_keyword_tags         — up to 3 niche keywords to add
  suggest_titles           — specific titles to add to the search
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_EVAL_SYSTEM_PROMPT = """You are a recruiting quality analyst for a job outreach platform.

Given a candidate profile and a sample of leads returned by Apollo (the hiring-manager search tool),
assess whether these leads actually match what the candidate is looking for.

Focus on: company stage fit (startup vs enterprise), industry/sector fit, role title relevance.

Apollo size ranges for reference: "1,50" = seed/tiny, "51,200" = small, "201,1000" = mid, "1001,10000" = large enterprise.

IMPORTANT: For early-stage startup candidates, seeing a Founder/CEO/CTO at a 10-person AI company
is a GOOD lead. Seeing an Operations Manager at a 5000-person FMCG company is a BAD lead.

Respond ONLY with valid JSON — no markdown, no prose, no code fences:
{
  "quality_score": <integer 1-10, where 10 = perfect match, 1 = completely wrong>,
  "main_issue": "<1 sentence describing the biggest problem, or null if quality is good>",
  "company_evaluations": [
    {"company": "<company name>", "fit_score": <1-10>, "reason": "<one phrase>"},
    ...
  ],
  "explicit_exclusions": <list of company names to hard-block from full collection, or null>,
  "restrict_company_sizes": <list of Apollo size ranges to use, e.g. ["1,50","51,200"], or null if no change>,
  "add_keyword_tags": <list of up to 3 lowercase keyword tags to add, e.g. ["ai","saas"], or null>,
  "suggest_titles": <list of up to 5 specific hiring-manager titles to add to the search, or null>
}"""


def evaluate_probe_with_llm(
    probe_leads: list[dict],
    candidate_prefs: dict,
) -> dict[str, Any]:
    """Call Azure OpenAI to evaluate probe lead quality and suggest filter adjustments.

    Args:
        probe_leads: List of parsed people dicts from Apollo page 1.
        candidate_prefs: Dict with keys: company_stage, niche_keywords, preferred_roles,
                         archetype_label, company_type_avoid.

    Returns:
        Dict with keys: quality_score, main_issue, company_evaluations,
        explicit_exclusions, restrict_company_sizes, add_keyword_tags, suggest_titles.
        Returns a safe default (quality_score=7, no adjustments) on any LLM failure.
    """
    from openai import AzureOpenAI
    from core.config import settings

    # Build a compact table of probe leads (title @ company, industry, size)
    rows = []
    for p in probe_leads[:25]:
        rows.append(
            f"- {p.get('title') or '?'} @ {p.get('company') or '?'} "
            f"(industry: {p.get('industry') or '?'}, size: {p.get('company_size') or '?'})"
        )
    leads_text = "\n".join(rows) if rows else "(no leads returned)"

    # Extract candidate context
    stage_raw = str(
        (candidate_prefs.get("company_stage") or ["any"])[0]
        if isinstance(candidate_prefs.get("company_stage"), list)
        else (candidate_prefs.get("company_stage") or "any")
    )
    niche_kws = candidate_prefs.get("niche_keywords") or []
    preferred_roles = candidate_prefs.get("preferred_roles") or []
    archetype = (candidate_prefs.get("archetype_label") or "").strip()
    avoid = candidate_prefs.get("company_type_avoid") or []

    candidate_text = (
        f"Company stage preference: {stage_raw}\n"
        f"Niche / industry focus: {', '.join(niche_kws) if niche_kws else 'none specified'}\n"
        f"Target roles: {', '.join(preferred_roles[:5]) if preferred_roles else 'not specified'}\n"
        f"Profile archetype: {archetype if archetype else 'not specified'}\n"
        f"Company types to avoid: {', '.join(avoid) if avoid else 'none'}"
    )

    user_content = (
        f"CANDIDATE PROFILE:\n{candidate_text}\n\n"
        f"PROBE LEADS ({len(rows)} results from Apollo page 1):\n{leads_text}\n\n"
        "Are these leads a good match for this candidate? "
        "Rate each company individually and suggest specific filter adjustments if needed."
    )

    try:
        client = AzureOpenAI(
            api_key=settings.AZURE_OPENAI_KEY,
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_version=settings.AZURE_OPENAI_API_VERSION,
        )
        response = client.chat.completions.create(
            model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _EVAL_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_completion_tokens=600,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Strip markdown fences if the model included them anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:].strip()
        result = json.loads(raw)
        logger.info(
            "[QualityProbe] LLM eval: score=%s issue=%r restrict=%s kws=%s titles=%s exclusions=%s",
            result.get("quality_score"),
            result.get("main_issue"),
            result.get("restrict_company_sizes"),
            result.get("add_keyword_tags"),
            result.get("suggest_titles"),
            result.get("explicit_exclusions"),
        )
        return result
    except Exception as exc:
        logger.warning("[QualityProbe] LLM evaluation failed (%s) — proceeding without adjustment", exc)
        return {
            "quality_score": 7,
            "main_issue": None,
            "company_evaluations": [],
            "explicit_exclusions": None,
            "restrict_company_sizes": None,
            "add_keyword_tags": None,
            "suggest_titles": None,
        }
