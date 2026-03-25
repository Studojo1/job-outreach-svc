"""Confidence scoring engine.

Produces a 0-100 score based on 3 signals:
  - Answer consistency (40%): do psychometric answers point the same direction?
  - Resume alignment (30%): does the dominant dimension match the resume?
  - Completion rate (30%): how many psychometric questions were answered?
"""

from __future__ import annotations
from .types import DIMENSIONS

_WEIGHTS = {"consistency": 0.40, "alignment": 0.30, "completion": 0.30}


def calculate_confidence(profile: dict) -> float:
    """Return a 0-100 confidence score and store it on the profile."""
    components = {
        "consistency": _answer_consistency(profile),
        "alignment": _resume_alignment(profile),
        "completion": _completion_rate(profile),
    }
    score = round(sum(components[k] * _WEIGHTS[k] for k in _WEIGHTS), 1)
    profile["confidence"] = score
    return score


def _answer_consistency(profile: dict) -> float:
    """How consistently do answers point to the same 1-2 dimensions?

    High consistency → the person has a clear profile.
    Low consistency → signals are scattered (not bad, just less certain).
    """
    answers = profile.get("answers") or []
    if len(answers) < 2:
        return 50.0

    # Count which dimension each answer's top weight points to
    dimension_votes: dict[str, int] = {d: 0 for d in DIMENSIONS}
    for a in answers:
        weights = a.get("weights") or {}
        if weights:
            top_dim = max(weights, key=weights.get)
            dimension_votes[top_dim] += 1

    total = sum(dimension_votes.values()) or 1
    top_count = max(dimension_votes.values())
    second_count = sorted(dimension_votes.values(), reverse=True)[1] if len(dimension_votes) > 1 else 0

    # Consistent = top 2 dimensions account for most answers
    top2_ratio = (top_count + second_count) / total
    return min(top2_ratio * 100, 100.0)


def _resume_alignment(profile: dict) -> float:
    """Do the psychometric scores align with what the resume suggests?

    Strong alignment = answers confirm resume signals.
    Weak alignment = either no resume or answers diverge from resume domain.
    """
    resume = profile.get("resume_data") or {}
    scores = profile.get("scores") or {}

    if not resume or not any(scores.values()):
        return 50.0  # neutral when no resume

    domain = (resume.get("domain") or "").lower()
    skills = " ".join(s.lower() for s in (resume.get("top_skills") or []))
    combined = domain + " " + skills

    DOMAIN_TO_DIM: dict[str, str] = {
        "software": "analytical", "data": "analytical", "finance": "analytical",
        "engineering": "analytical", "research": "analytical", "analytics": "analytical",
        "design": "creative", "product": "creative", "content": "creative",
        "media": "creative", "ux": "creative",
        "operations": "execution", "project": "execution", "supply": "execution",
        "logistics": "execution", "manufacturing": "execution",
        "sales": "social", "marketing": "social", "hr": "social",
        "recruitment": "social", "partnerships": "social",
    }

    expected_dim = None
    for keyword, dim in DOMAIN_TO_DIM.items():
        if keyword in combined:
            expected_dim = dim
            break

    if not expected_dim:
        return 60.0  # slightly above neutral for non-matching domains

    # Check if expected dimension is in top 2
    sorted_dims = sorted(scores.items(), key=lambda x: -x[1])
    top2_dims = [d for d, _ in sorted_dims[:2]]

    if expected_dim == top2_dims[0]:
        return 95.0  # perfect alignment
    elif expected_dim in top2_dims:
        return 75.0  # decent alignment
    else:
        return 35.0  # divergence — not wrong, just different


def _completion_rate(profile: dict) -> float:
    """More psychometric answers = higher confidence."""
    count = len(profile.get("answers") or [])
    max_q = 5  # we serve 5 psychometric questions
    return min((count / max_q) * 100, 100.0)
