"""
Signal extractor — pure keyword/NLP matching on quiz text answers.
No ML models, no LLM. All operations < 5ms.

Used to extract structured signals from free-text quiz answers for:
- Payload builder context
- Downstream lead scoring filters
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Keyword maps
# ---------------------------------------------------------------------------

_ROLE_KEYWORDS: dict[str, list[str]] = {
    "software_engineering": [
        "software", "developer", "engineer", "backend", "frontend", "full stack",
        "fullstack", "swe", "sde", "coding", "programming", "devops", "cloud",
        "mobile", "ios", "android", "react", "python", "java", "golang",
    ],
    "data": [
        "data", "analytics", "analyst", "ml", "machine learning", "ai", "nlp",
        "deep learning", "data science", "bi", "business intelligence", "sql",
        "tableau", "power bi", "statistics",
    ],
    "product": [
        "product", "pm", "product manager", "product management", "roadmap",
        "user research", "ux research", "product strategy",
    ],
    "design": [
        "design", "ux", "ui", "user experience", "figma", "sketch", "visual",
        "graphic", "brand design", "motion",
    ],
    "marketing": [
        "marketing", "growth", "content", "seo", "sem", "paid ads", "social media",
        "brand", "campaigns", "demand gen", "lifecycle", "crm", "email marketing",
        "performance marketing",
    ],
    "sales": [
        "sales", "business development", "bd", "account executive", "ae",
        "sdr", "bdr", "revenue", "closing", "enterprise sales",
    ],
    "finance": [
        "finance", "investment", "banking", "ib", "investment banking",
        "private equity", "pe", "venture capital", "vc", "accounting",
        "financial modelling", "cfa",
    ],
    "operations": [
        "operations", "ops", "supply chain", "logistics", "process",
        "program management", "project management", "consulting",
    ],
    "hr_people": [
        "hr", "human resources", "recruiting", "talent", "people ops",
        "people operations",
    ],
    "legal": ["legal", "law", "attorney", "counsel", "compliance", "regulatory"],
}

_MOTIVATION_KEYWORDS: dict[str, list[str]] = {
    "learning": ["learn", "skill", "grow", "knowledge", "training", "mentor", "develop"],
    "impact": ["impact", "matter", "change", "mission", "purpose", "meaningful", "difference"],
    "money": ["salary", "ctc", "lpa", "pay", "compensation", "package", "earn", "money"],
    "title_prestige": ["title", "promotion", "senior", "lead", "manager", "recognition", "brand name"],
    "autonomy": ["autonomy", "independent", "own", "freedom", "flexibility", "remote"],
    "challenge": ["challenge", "hard problem", "difficult", "stretch", "ambitious"],
    "stability": ["stable", "stability", "security", "safe", "established"],
    "startup_energy": ["startup", "early stage", "hustle", "fast paced", "build from scratch"],
}

_TRAIT_KEYWORDS: dict[str, list[str]] = {
    "technical": ["technical", "engineer", "code", "build", "system", "architecture"],
    "creative": ["creative", "design", "write", "content", "storytelling", "brand"],
    "analytical": ["data", "analysis", "research", "insight", "metric", "measure"],
    "people_oriented": ["people", "team", "collaborate", "communicate", "lead", "mentor"],
    "entrepreneurial": ["founder", "start", "venture", "own something", "build a company"],
}

_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "tech": ["tech", "software", "saas", "startup", "internet", "platform"],
    "fintech": ["fintech", "finance", "banking", "payments", "neobank", "crypto", "web3"],
    "healthtech": ["health", "healthcare", "medical", "biotech", "pharma", "femtech"],
    "edtech": ["edtech", "education", "learning", "school", "university"],
    "ecommerce": ["ecommerce", "e-commerce", "retail", "d2c", "direct to consumer"],
    "media": ["media", "content", "journalism", "newsletter", "publishing", "podcast"],
    "climate": ["climate", "sustainability", "green", "clean energy", "carbon", "esg"],
    "gaming": ["gaming", "game", "esports", "entertainment"],
    "consulting": ["consulting", "mckinsey", "bcg", "bain", "deloitte", "pwc", "kpmg"],
    "enterprise": ["enterprise", "b2b", "corporate", "fortune 500"],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_signals(text: str) -> dict:
    """
    Extract structured signals from a single free-text answer.
    Returns dict with keys: role_interest, motivation, traits, industry_interest.
    """
    lower = text.lower()
    return {
        "role_interest": _match_keys(lower, _ROLE_KEYWORDS),
        "motivation": _match_keys(lower, _MOTIVATION_KEYWORDS),
        "traits": _match_keys(lower, _TRAIT_KEYWORDS),
        "industry_interest": _match_keys(lower, _INDUSTRY_KEYWORDS),
    }


def update_user_profile(profile: dict, new_signals: dict) -> dict:
    """
    Merge new signals into the cumulative user_profile dict (union, deduplicated).
    """
    for key in ("role_interest", "motivation", "traits", "industry_interest"):
        existing = set(profile.get(key, []))
        existing.update(new_signals.get(key, []))
        profile[key] = list(existing)
    return profile


def compute_clarity_score(answers: list[str]) -> float:
    """
    0.0–1.0 clarity score based on how specific and detailed the answers are.
    Simple heuristic: avg word count normalised to 0–1 (caps at 30 words).
    """
    if not answers:
        return 0.0
    total_words = sum(len(a.split()) for a in answers if a)
    avg = total_words / len(answers)
    return min(avg / 30.0, 1.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_keys(lower_text: str, keyword_map: dict[str, list[str]]) -> list[str]:
    matched = []
    for category, keywords in keyword_map.items():
        if any(kw in lower_text for kw in keywords):
            matched.append(category)
    return matched
