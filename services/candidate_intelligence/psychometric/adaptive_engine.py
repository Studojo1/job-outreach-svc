"""Adaptive question selection engine.

Picks the best 5 psychometric questions from the pool of 8, based on:
1. Which dimensions are under-covered by onboarding signals
2. Resume background bias (tech resume → don't over-test analytical)
3. Dimension diversity — avoid hammering the same dimension twice in a row

Called once after Q8 (onboarding complete) to build the psychometric sequence.
"""

from __future__ import annotations
from .types import DIMENSIONS, empty_scores
from .questions import PSYCHOMETRIC_QUESTIONS
from .scoring_engine import extract_onboarding_signals

TARGET_PSYCHOMETRIC_COUNT = 5  # serve 5 out of 8


def select_psychometric_questions(
    onboarding_answers: dict,
    resume_data: dict | None = None,
) -> list[dict]:
    """Select the best psychometric questions to ask.

    Returns a list of question dicts (from questions.py) in optimal order.
    """
    # Step 1: Get onboarding signals to see what's already known
    ob_signals = extract_onboarding_signals(onboarding_answers)

    # Step 2: Get resume dimension hints
    resume_hints = _resume_dimension_hints(resume_data or {})

    # Step 3: Identify under-covered dimensions
    combined = empty_scores()
    for dim in DIMENSIONS:
        combined[dim] = ob_signals.get(dim, 0) + resume_hints.get(dim, 0)

    # Dimensions with weakest signal need more questions
    dim_need = {}
    max_signal = max(combined.values()) if any(combined.values()) else 1.0
    for dim in DIMENSIONS:
        dim_need[dim] = max(0, max_signal - combined[dim])

    # Step 4: Score each question by how much new info it provides
    scored = []
    for q in PSYCHOMETRIC_QUESTIONS:
        priority = _question_priority(q, dim_need, combined)
        scored.append((priority, q))

    scored.sort(key=lambda x: -x[0])

    # Step 5: Pick top N, then re-order for dimension diversity
    selected = [q for _, q in scored[:TARGET_PSYCHOMETRIC_COUNT]]
    return _diversify_order(selected)


def _resume_dimension_hints(resume_data: dict) -> dict[str, float]:
    """Extract weak dimension signals from resume to avoid redundant questions."""
    domain = (resume_data.get("domain") or "").lower()
    hints = empty_scores()

    DOMAIN_MAP = {
        "analytical": ["software", "data", "finance", "engineering", "analytics", "research"],
        "creative": ["design", "product", "content", "media", "creative", "marketing"],
        "execution": ["operations", "project", "supply", "logistics", "manufacturing"],
        "social": ["sales", "hr", "recruitment", "partnerships", "marketing", "communication"],
    }

    for dim, keywords in DOMAIN_MAP.items():
        for kw in keywords:
            if kw in domain:
                hints[dim] += 0.5  # weak signal — don't overweight

    return hints


def _question_priority(q: dict, dim_need: dict[str, float], current: dict[str, float]) -> float:
    """Score how useful a question is given current knowledge gaps."""
    primary_dims = q.get("primary_dimensions") or []

    # Sum up the need across dimensions this question tests
    need_score = sum(dim_need.get(d, 0) for d in primary_dims)

    # Bonus for questions that test multiple dimensions (more info per question)
    cross_dim_options = 0
    for opt in q["options"]:
        if len(opt.get("weights", {})) > 1:
            cross_dim_options += 1
    diversity_bonus = cross_dim_options * 0.5

    # Penalty if this question's dimensions are already well-covered
    saturation_penalty = 0
    for d in primary_dims:
        if current.get(d, 0) > 2.0:
            saturation_penalty += 0.5

    return need_score + diversity_bonus - saturation_penalty


def _diversify_order(questions: list[dict]) -> list[dict]:
    """Reorder so consecutive questions don't test the same primary dimension."""
    if len(questions) <= 2:
        return questions

    ordered = [questions[0]]
    remaining = questions[1:]

    while remaining:
        last_dims = set(ordered[-1].get("primary_dimensions") or [])
        # Pick the next question with least overlap to the last one
        best_idx, best_overlap = 0, 999
        for i, q in enumerate(remaining):
            overlap = len(last_dims & set(q.get("primary_dimensions") or []))
            if overlap < best_overlap:
                best_overlap = overlap
                best_idx = i
        ordered.append(remaining.pop(best_idx))

    return ordered
