"""Career Strategist Agent — LLM-powered Apollo search strategy generation.

Takes the candidate's full profile (resume intelligence + quiz answers + flex notes)
and produces a structured SearchStrategy JSON that drives Apollo filter construction.

Replaces the rules-based 5-layer title engine with an LLM that reasons about which
hiring managers would actually respond to this specific candidate's outreach.

Falls back to None on any failure — filter_generator_service.py then uses the
existing rules-based pipeline as a safe fallback. No user-visible latency impact.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Apollo hard constraints — validated against before any value reaches the API.
APOLLO_VALID_SENIORITY_CODES = frozenset([
    "owner", "founder", "c_suite", "partner", "vp",
    "head", "director", "manager", "senior", "entry", "intern",
])
APOLLO_VALID_SIZE_RANGES = frozenset([
    "1,50", "51,200", "201,1000", "1001,10000",
])

_SYSTEM_PROMPT = """You are a senior partner at a top executive search firm with 20 years of experience placing candidates at high-growth technology companies. You specialize in finding the exact hiring managers who will respond to a candidate's cold outreach.

Your task: given a candidate's profile, design a precise Apollo.io hiring-manager search strategy. Apollo is a B2B contact database — you are designing API search filters to find decision makers, NOT job postings.

HARD APOLLO CONSTRAINTS — violating these will break the API call:
1. Person seniority codes — use ONLY these exact strings (no others):
   owner, founder, c_suite, partner, vp, head, director, manager, senior, entry, intern
2. Company size ranges — use ONLY these exact strings (no others):
   "1,50"  "51,200"  "201,1000"  "1001,10000"
3. Person titles: natural English hiring-manager titles (Apollo does substring match).
   Generate titles for the DECISION MAKER who would hire this candidate, not the role itself.
   "Head of Product" will match "Global Head of Product", "VP of Product & Design", etc.
4. Keywords: lowercase, 1-3 words, Apollo company tag taxonomy (e.g. "saas", "edtech",
   "fintech", "ai", "b2b", "consumer", "marketplace", "deeptech", "proptech").

SENIORITY SELECTION RULE:
The person_seniorities list MUST be the UNION of all seniority codes across all your
target_size_ranges. Example: if you target "1,50" (needs founder/c_suite/vp) and "51,200"
(needs vp/head/director), your person_seniorities = ["owner","founder","c_suite","partner","vp","head","director"].
Err on the side of including more codes — Apollo applies this as an OR filter.

TITLE STRATEGY RULES:
- For seed companies (1,50): founders, CEOs, CTOs, CPOs ARE the hiring managers. Include them.
  Do NOT exclude c-suite/founders because the candidate is junior — at a 15-person startup
  the CEO is literally the person doing the hiring.
- For small (51,200): VPs, Heads, Directors make hiring decisions.
- For mid/large (201,1000+): Directors and Managers typical.
- Generate 1-3 clusters per candidate. Each cluster groups related decision-maker roles by function.
- ALWAYS consider a "founders_office" cluster for candidates with builder/founding/generalist signals:
  Chief of Staff, Head of Special Projects, Head of Strategy — these people respond well to
  ambitious candidates who can pitch execution.
- Per cluster: 3-8 titles per size range. Total unique titles across all clusters: aim 20-50.
  More than 60 total titles degrades Apollo result quality.
- Avoid overly generic titles like "Manager", "Director" alone — be function-specific.

Respond ONLY with valid JSON — no markdown fences, no prose, no explanation outside the JSON:
{
  "title_clusters": [
    {
      "cluster_name": "snake_case_short_name",
      "rationale": "1-2 sentences: why this cluster fits this specific candidate",
      "priority": 1,
      "titles_by_size": {
        "1,50": ["Title A", "Title B", "Title C"],
        "51,200": ["Title A", "Title B"],
        "201,1000": ["Title A", "Title B"]
      }
    }
  ],
  "target_size_ranges": ["1,50", "51,200"],
  "person_seniorities": ["owner", "founder", "c_suite", "vp", "head", "director"],
  "keyword_strategy": ["saas", "ai"],
  "candidate_compelling_angle": "1-2 sentences: what makes this candidate's pitch compelling to these specific hiring managers — used in email generation",
  "search_rationale": "2-3 sentences: why this strategy will yield qualified leads for this candidate"
}"""


def _build_user_message(
    resume_profile: dict,
    quiz_prefs: dict,
    preferred_roles: list[str],
    flex_notes: dict | None,
) -> str:
    """Assemble the user-turn message for the Career Strategist LLM call."""
    archetype = (resume_profile or {}).get("archetype_label", "not detected")
    likely_roles = (resume_profile or {}).get("likely_roles", [])
    company_avoid = (resume_profile or {}).get("company_type_avoid", [])
    seniority_signal = (resume_profile or {}).get("seniority_signal", "")
    builder_signal = (resume_profile or {}).get("builder_signal", False)
    distribution_signal = (resume_profile or {}).get("distribution_signal", False)

    company_stage = str(
        (quiz_prefs.get("company_stage") or ["any"])[0]
        if isinstance(quiz_prefs.get("company_stage"), list)
        else (quiz_prefs.get("company_stage") or "any")
    )
    niche_keywords = quiz_prefs.get("niche_keywords") or []
    tech_stack = quiz_prefs.get("tech_stack") or []
    work_mode = quiz_prefs.get("work_mode") or "flexible"
    industry_interests = quiz_prefs.get("industry_interests") or []
    clarity = quiz_prefs.get("clarity") or "medium"
    career_stage = quiz_prefs.get("career_stage") or "not specified"

    flex_project = ""
    flex_outcome = ""
    if flex_notes:
        flex_project = (flex_notes.get("best_project") or "").strip()
        flex_outcome = (flex_notes.get("outcome") or "").strip()

    lines = [
        "CANDIDATE PROFILE",
        "─────────────────",
        f"Career stage: {career_stage}",
        f"Profile archetype: {archetype}",
        f"Seniority signal: {seniority_signal or 'not detected'}",
        f"Builder signal: {'yes' if builder_signal else 'no'}",
        f"Distribution / growth signal: {'yes' if distribution_signal else 'no'}",
        f"Preferred roles (from resume + quiz): {', '.join(preferred_roles[:6]) if preferred_roles else 'not specified'}",
        f"Likely roles detected from resume: {', '.join(likely_roles[:5]) if likely_roles else 'none'}",
        "",
        "QUIZ ANSWERS",
        "────────────",
        f"Company stage preference: {company_stage}",
        f"Clarity level: {clarity}",
        f"Work mode preference: {work_mode}",
        f"Industry interests: {', '.join(industry_interests[:5]) if industry_interests else 'none specified'}",
        f"Niche focus keywords: {', '.join(niche_keywords[:5]) if niche_keywords else 'none specified'}",
        f"Tech stack: {', '.join(tech_stack[:5]) if tech_stack else 'none specified'}",
        f"Company types to avoid: {', '.join(company_avoid[:5]) if company_avoid else 'none specified'}",
    ]

    if flex_project:
        lines += [
            "",
            "CANDIDATE'S OWN WORDS (from onboarding — high signal)",
            "──────────────────────────────────────────────────────",
            f"Best project: {flex_project[:400]}",
        ]
    if flex_outcome:
        lines += [f"Outcome / impact: {flex_outcome[:300]}"]

    lines += [
        "",
        "TASK",
        "────",
        "Design the Apollo hiring-manager search strategy for this candidate. "
        "Think about who would actually read this person's cold outreach and respond. "
        "Return only the JSON schema specified.",
    ]

    return "\n".join(lines)


def _validate_strategy(raw: dict) -> dict:
    """Validate and sanitize Career Strategist output against Apollo constraints.

    Strips invalid seniority codes and size ranges. Caps titles per cluster.
    Returns a cleaned dict — may have empty clusters if LLM hallucinated sizes.
    """
    validated = {}

    # Validate title_clusters
    raw_clusters = raw.get("title_clusters") or []
    clean_clusters = []
    for cluster in raw_clusters:
        titles_by_size = cluster.get("titles_by_size") or {}
        clean_tbs = {}
        for size, titles in titles_by_size.items():
            if size not in APOLLO_VALID_SIZE_RANGES:
                logger.warning("[CareerStrategist] Invalid size range %r — skipped", size)
                continue
            if isinstance(titles, list):
                # Cap at 10 per size range; strip non-strings
                clean_tbs[size] = [str(t).strip() for t in titles if t and str(t).strip()][:10]
        if clean_tbs:
            clean_clusters.append({
                "cluster_name": str(cluster.get("cluster_name", "cluster"))[:50],
                "rationale": str(cluster.get("rationale", ""))[:300],
                "priority": int(cluster.get("priority") or 1),
                "titles_by_size": clean_tbs,
            })
    validated["title_clusters"] = clean_clusters

    # Validate target_size_ranges
    raw_sizes = raw.get("target_size_ranges") or []
    validated["target_size_ranges"] = [
        s for s in raw_sizes if s in APOLLO_VALID_SIZE_RANGES
    ]
    if not validated["target_size_ranges"]:
        # Default to broad if LLM produced nothing valid
        validated["target_size_ranges"] = ["1,50", "51,200", "201,1000"]

    # Validate person_seniorities
    raw_seniorities = raw.get("person_seniorities") or []
    validated["person_seniorities"] = [
        s for s in raw_seniorities if s in APOLLO_VALID_SENIORITY_CODES
    ]
    if not validated["person_seniorities"]:
        validated["person_seniorities"] = ["vp", "head", "director", "manager"]

    # Validate keywords (lowercase strings, max 5)
    raw_kws = raw.get("keyword_strategy") or []
    validated["keyword_strategy"] = [
        str(k).strip().lower() for k in raw_kws if k and str(k).strip()
    ][:5]

    # Pass through text fields
    validated["candidate_compelling_angle"] = str(raw.get("candidate_compelling_angle") or "")[:500]
    validated["search_rationale"] = str(raw.get("search_rationale") or "")[:500]

    return validated


def run_career_strategist(
    resume_profile: dict,
    quiz_prefs: dict,
    preferred_roles: list[str],
    flex_notes: dict | None = None,
) -> dict[str, Any] | None:
    """Call Azure OpenAI to generate a structured Apollo search strategy.

    Args:
        resume_profile: candidates.resume_profile (archetype, signals, etc.)
        quiz_prefs: parsed_json.preferences (company_stage, niche_keywords, etc.)
        preferred_roles: list of role title strings for this candidate
        flex_notes: candidates.flex_notes (best_project, outcome) — optional

    Returns:
        Validated strategy dict, or None on any failure (caller falls back to
        rules-based filter generation).
    """
    from openai import AzureOpenAI
    from core.config import settings

    user_message = _build_user_message(resume_profile, quiz_prefs, preferred_roles, flex_notes)

    try:
        client = AzureOpenAI(
            api_key=settings.AZURE_OPENAI_KEY,
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_version=settings.AZURE_OPENAI_API_VERSION,
        )
        response = client.chat.completions.create(
            model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0,
            max_completion_tokens=1200,
        )
        raw_text = (response.choices[0].message.content or "").strip()

        # Strip markdown fences if model included them
        if raw_text.startswith("```"):
            parts = raw_text.split("```")
            raw_text = parts[1] if len(parts) > 1 else raw_text
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()

        raw = json.loads(raw_text)
        strategy = _validate_strategy(raw)

        cluster_names = [c["cluster_name"] for c in strategy["title_clusters"]]
        total_titles = sum(
            len(titles)
            for c in strategy["title_clusters"]
            for titles in c["titles_by_size"].values()
        )
        logger.info(
            "[CareerStrategist] Strategy generated: clusters=%s sizes=%s seniorities=%s "
            "keywords=%s total_titles=%d",
            cluster_names,
            strategy["target_size_ranges"],
            strategy["person_seniorities"],
            strategy["keyword_strategy"],
            total_titles,
        )
        return strategy

    except Exception as exc:
        logger.warning(
            "[CareerStrategist] Failed (%s) — falling back to rules-based filter generation",
            exc,
        )
        return None
