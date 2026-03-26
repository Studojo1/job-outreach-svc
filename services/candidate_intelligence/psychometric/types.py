"""Core types for the psychometric profiling system."""

from __future__ import annotations
from typing import Literal

Dimension = Literal[
    "analytical", "creative", "execution", "social",
    "leadership", "strategic", "technical", "communication",
]
DIMENSIONS: list[Dimension] = [
    "analytical", "creative", "execution", "social",
    "leadership", "strategic", "technical", "communication",
]


def empty_scores() -> dict[str, float]:
    return {
        "analytical": 0.0,
        "creative": 0.0,
        "execution": 0.0,
        "social": 0.0,
        "leadership": 0.0,
        "strategic": 0.0,
        "technical": 0.0,
        "communication": 0.0,
    }


def new_profile(
    resume_data: dict | None = None,
    onboarding_answers: dict | None = None,
) -> dict:
    """Create a fresh psychometric profile dict."""
    return {
        "scores": empty_scores(),
        "answers": [],          # list of {"question_id": str, "selected": str, "weights": dict}
        "traits": [],           # detected trait labels
        "confidence": 0.0,
        "resume_data": resume_data or {},
        "onboarding_answers": onboarding_answers or {},
    }
