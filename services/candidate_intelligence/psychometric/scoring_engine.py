"""Multi-dimensional scoring engine.

Tracks 8 dimensions: analytical, creative, execution, social,
leadership, strategic, technical, communication.
Signals come from 3 sources (in order of trust):
  1. Psychometric answers (direct, highest weight)
  2. Onboarding answers (indirect, moderate weight)
  3. Resume data (background, capped at 15% influence)
"""

from __future__ import annotations
from .types import DIMENSIONS, empty_scores

# ── Onboarding answer → dimension mapping ─────────────────────────────────
# These extract dimensional signals from the Q1-Q8 onboarding answers
# before psychometric questions even start.

_MOTIVATION_WEIGHTS: dict[str, dict[str, float]] = {
    "learning fast":     {"analytical": 1.0, "creative": 0.5},
    "building new skills": {"analytical": 1.0, "creative": 0.5, "technical": 0.5},
    "real impact":       {"execution": 1.0, "analytical": 0.5},
    "impact on the product": {"execution": 1.0, "strategic": 0.5},
    "compensation":      {"execution": 1.0},
    "career growth":     {"execution": 1.0, "leadership": 0.5},
    "high-energy team":  {"social": 1.5, "leadership": 0.5},
    "clear mission":     {"creative": 0.5, "strategic": 0.5},
    "autonomy":          {"analytical": 1.0, "creative": 0.5},
    "independently":     {"analytical": 1.0, "technical": 0.5},
}

_COMPANY_STAGE_WEIGHTS: dict[str, dict[str, float]] = {
    "early":      {"creative": 1.0, "execution": 0.5, "strategic": 0.5},
    "seed":       {"creative": 1.0, "execution": 0.5, "technical": 0.5},
    "growth":     {"execution": 1.0, "creative": 0.5, "leadership": 0.5},
    "mid-size":   {"execution": 0.5, "social": 0.5, "leadership": 0.5},
    "large":      {"analytical": 0.5, "execution": 0.5, "communication": 0.5},
    "enterprise": {"analytical": 0.5, "strategic": 0.5, "communication": 0.5},
    "mnc":        {"analytical": 0.5, "social": 0.5, "communication": 0.5},
}

_CAREER_GOAL_KEYWORDS: dict[str, dict[str, float]] = {
    "data": {"analytical": 1.0},
    "analy": {"analytical": 1.0},
    "research": {"analytical": 0.8},
    "model": {"analytical": 0.8, "technical": 0.5},
    "design": {"creative": 1.0},
    "build": {"creative": 0.8, "execution": 0.5, "technical": 0.5},
    "create": {"creative": 0.8, "communication": 0.5},
    "launch": {"execution": 1.0, "strategic": 0.5},
    "ship": {"execution": 1.0, "technical": 0.5},
    "manage": {"execution": 0.8, "leadership": 0.8},
    "lead": {"leadership": 1.5, "social": 0.5},
    "sell": {"social": 1.0, "communication": 0.5},
    "close": {"social": 0.8, "execution": 0.5},
    "market": {"creative": 0.5, "communication": 0.8},
    "strategy": {"strategic": 1.2, "analytical": 0.5},
    "consult": {"strategic": 1.0, "communication": 0.5},
    "gtm": {"execution": 0.8, "strategic": 0.5},
    "ops": {"execution": 1.0},
    "operations": {"execution": 1.0, "leadership": 0.3},
    "content": {"creative": 1.0, "communication": 1.0},
    "product": {"creative": 0.5, "analytical": 0.5, "strategic": 0.5},
    "code": {"technical": 1.5, "analytical": 0.5},
    "engineer": {"technical": 1.5, "analytical": 0.5},
    "software": {"technical": 1.5},
    "develop": {"technical": 1.0},
    "write": {"communication": 1.0, "creative": 0.5},
    "present": {"communication": 1.0},
    "pitch": {"communication": 0.8, "social": 0.5},
    "vision": {"strategic": 1.0, "leadership": 0.5},
    "plan": {"strategic": 1.0, "analytical": 0.5},
    "team": {"leadership": 0.8, "social": 0.5},
    "hire": {"leadership": 0.8, "social": 0.5},
}

# ── Resume domain → dimension mapping (max 15% boost) ────────────────────

_RESUME_DIMENSION_KEYWORDS: dict[str, list[str]] = {
    "analytical": [
        "data", "analytics", "finance", "research", "statistics", "sql",
        "modeling", "quantitative", "actuarial", "economics",
    ],
    "creative": [
        "design", "product", "content", "ux", "branding", "innovation",
        "creative", "media", "storytelling", "copywriting",
    ],
    "execution": [
        "operations", "project management", "supply chain", "process",
        "agile", "scrum", "shipping", "logistics", "implementation",
    ],
    "social": [
        "sales", "marketing", "hr", "recruiting",
        "negotiation", "partnerships", "account",
    ],
    "leadership": [
        "leadership", "manager", "director", "head of", "team lead",
        "vp", "managing", "mentoring", "people management",
    ],
    "strategic": [
        "strategy", "consulting", "advisory", "business development",
        "planning", "roadmap", "go-to-market", "gtm", "strategic",
    ],
    "technical": [
        "software", "engineering", "python", "javascript", "react",
        "backend", "frontend", "api", "cloud", "devops", "programming",
        "developer", "typescript", "node", "sql",
    ],
    "communication": [
        "writing", "copywriting", "communications", "journalism",
        "public relations", "presenting", "content creation",
        "storytelling", "editorial", "newsletter",
    ],
}


def extract_onboarding_signals(onboarding_answers: dict) -> dict[str, float]:
    """Extract dimensional signals from Q1-Q8 onboarding answers.

    Returns a scores dict (not normalized — raw signal accumulation).
    """
    scores = empty_scores()

    # Work motivation (Q8) — strongest onboarding signal
    motivation = (onboarding_answers.get("work_motivation") or "").lower()
    for keyword, weights in _MOTIVATION_WEIGHTS.items():
        if keyword in motivation:
            for dim, w in weights.items():
                scores[dim] += w

    # Company stage (Q4) — moderate signal
    company = (onboarding_answers.get("company_stage") or "").lower()
    for keyword, weights in _COMPANY_STAGE_WEIGHTS.items():
        if keyword in company:
            for dim, w in weights.items():
                scores[dim] += w
            break  # only match one

    # Career goal (Q5) — free text, keyword scan
    goal = (onboarding_answers.get("career_goal") or "").lower()
    for keyword, weights in _CAREER_GOAL_KEYWORDS.items():
        if keyword in goal:
            for dim, w in weights.items():
                scores[dim] += w

    return scores


def update_scores(profile: dict, question_id: str, weights: dict[str, float]) -> dict:
    """Add dimensional weights from a psychometric answer to the profile."""
    for dim, w in weights.items():
        if dim in profile["scores"]:
            profile["scores"][dim] += w

    profile["answers"].append({
        "question_id": question_id,
        "weights": weights,
    })
    return profile


def apply_resume_boost(profile: dict) -> dict:
    """Adjust scores by up to 15% based on resume alignment.

    This runs AFTER all psychometric answers are scored. It nudges
    scores toward what the resume supports, but never dominates.
    """
    resume = profile.get("resume_data") or {}
    domain = (resume.get("domain") or "").lower()
    skills = [s.lower() for s in (resume.get("top_skills") or [])]
    combined = domain + " " + " ".join(skills)

    if not combined.strip():
        return profile

    resume_signals = empty_scores()
    for dim, keywords in _RESUME_DIMENSION_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                resume_signals[dim] += 1.0

    total_signals = sum(resume_signals.values())
    if total_signals == 0:
        return profile

    max_boost = 0.15
    for dim in DIMENSIONS:
        ratio = (resume_signals[dim] / total_signals) * max_boost
        profile["scores"][dim] *= (1.0 + ratio)

    return profile


def normalize_scores(profile: dict) -> dict:
    """Normalize scores to 0-100 scale. Prevents extreme skew."""
    scores = profile["scores"]
    total = sum(scores.values())
    if total == 0:
        for dim in DIMENSIONS:
            scores[dim] = 25.0  # uniform
        return profile

    for dim in DIMENSIONS:
        scores[dim] = round((scores[dim] / total) * 100, 1)

    return profile


# ── Trait detection ───────────────────────────────────────────────────────

_TRAIT_RULES: list[dict] = [
    {
        "check": lambda s: s["analytical"] >= 45 and s["social"] < 20,
        "trait": "Independent Thinker",
        "label": "Deep Analyst",
    },
    {
        "check": lambda s: s["creative"] >= 45 and s["social"] >= 25,
        "trait": "Idea-Driven Communicator",
        "label": "Creative Builder",
    },
    {
        "check": lambda s: s["execution"] >= 45 and s["analytical"] >= 20,
        "trait": "Structured Executor",
        "label": "Structured Executor",
    },
    {
        "check": lambda s: s["social"] >= 45 and s["execution"] >= 20,
        "trait": "People-First Operator",
        "label": "People-First Leader",
    },
    {
        "check": lambda s: s["analytical"] >= 35 and s["creative"] >= 35,
        "trait": "Strategic Creative",
        "label": "Strategic Creative",
    },
    {
        "check": lambda s: s["execution"] >= 35 and s["social"] >= 35,
        "trait": "Operational Leader",
        "label": "Operational Leader",
    },
    {
        "check": lambda s: s["analytical"] >= 30 and s["execution"] >= 30,
        "trait": "Systematic Builder",
        "label": "Systematic Builder",
    },
    {
        "check": lambda s: s["creative"] >= 30 and s["execution"] >= 30,
        "trait": "Maker",
        "label": "Full-Stack Maker",
    },
    {
        "check": lambda s: max(s.values()) - min(s.values()) < 15,
        "trait": "Versatile Generalist",
        "label": "Versatile Generalist",
    },
]


def detect_traits(profile: dict) -> list[str]:
    """Detect personality traits from normalized scores. Returns 1-3 traits."""
    scores = profile["scores"]
    matched = []
    for rule in _TRAIT_RULES:
        try:
            if rule["check"](scores):
                matched.append(rule["trait"])
        except (KeyError, TypeError):
            continue

    # Deduplicate and cap
    seen, unique = set(), []
    for t in matched:
        if t not in seen:
            seen.add(t)
            unique.append(t)
        if len(unique) == 3:
            break

    profile["traits"] = unique or ["Emerging Professional"]
    return unique or ["Emerging Professional"]
