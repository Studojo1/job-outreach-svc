"""Result engine — maps dimensions to roles, generates human reasoning.

This is the final output layer. It takes a fully scored profile and produces:
  - Top 2 strengths (dimensions)
  - Trait labels
  - Recommended roles (blended from dimensions + resume)
  - Human-readable reasoning (not "you scored high in X")
  - Confidence score
"""

from __future__ import annotations
from .types import DIMENSIONS

# ── Dimension → Role mapping ─────────────────────────────────────────────
# Each dimension maps to roles sorted by specificity (most specific first).
# The engine picks from top 2 dimensions and blends.

ROLE_MAP: dict[str, list[str]] = {
    "analytical": [
        "Management Consultant (Analyst)",
        "Data Analyst",
        "Strategy Associate",
        "Financial Analyst",
        "Business Intelligence Analyst",
        "Research Analyst",
        "Quantitative Researcher",
        "Risk Analyst",
    ],
    "creative": [
        "Product Manager",
        "UX Designer",
        "Brand Strategist",
        "Content Strategist",
        "Growth Marketing Manager",
        "Innovation Lead",
        "Design Researcher",
        "Creative Strategist",
    ],
    "execution": [
        "Business Operations Associate",
        "Project Manager",
        "Program Coordinator",
        "Operations Manager",
        "Supply Chain Analyst",
        "Implementation Lead",
        "Process Improvement Analyst",
        "Scrum Master",
    ],
    "social": [
        "Sales Development Representative (SDR)",
        "Account Manager",
        "Partnership Manager",
        "Client Success Manager",
        "Community Manager",
        "Talent Acquisition Specialist",
        "HR Business Partner",
        "Recruitment Lead",
    ],
    "leadership": [
        "Team Lead",
        "Program Manager",
        "Chief of Staff",
        "Operations Director",
        "People Manager",
        "General Manager",
        "Product Lead",
        "VP of Operations",
    ],
    "strategic": [
        "Strategy Consultant",
        "Business Development Manager",
        "Go-to-Market Lead",
        "Corporate Strategy Analyst",
        "Market Research Manager",
        "Strategic Partnerships Lead",
        "M&A Analyst",
        "Policy Analyst",
    ],
    "technical": [
        "Software Engineer",
        "Full-Stack Developer",
        "Data Engineer",
        "Backend Developer",
        "DevOps Engineer",
        "Technical Product Manager",
        "Solutions Architect",
        "ML Engineer",
    ],
    "communication": [
        "Content Strategist",
        "Brand Manager",
        "Communications Manager",
        "Public Relations Manager",
        "Editorial Lead",
        "Growth Marketing Manager",
        "Copywriter",
        "Content Director",
    ],
}

# ── Cross-dimension specializations ──────────────────────────────────────
# When two dimensions are both strong, these hybrid roles are more accurate.

_CROSS_ROLES: dict[tuple[str, str], list[str]] = {
    ("analytical", "creative"): ["Product Analyst", "Strategy Consultant", "UX Researcher"],
    ("analytical", "execution"): ["Data Engineer", "Technical Program Manager", "Systems Analyst"],
    ("analytical", "social"): ["Management Consultant", "Research Director", "Technical Sales"],
    ("analytical", "strategic"): ["Corporate Strategy Analyst", "Management Consultant", "BI Consultant"],
    ("analytical", "technical"): ["Data Scientist", "ML Engineer", "Quantitative Analyst"],
    ("creative", "execution"): ["Product Manager", "Growth Lead", "Full-Stack Builder"],
    ("creative", "social"): ["Brand Manager", "Marketing Lead", "Content Director"],
    ("creative", "communication"): ["Content Strategist", "Creative Director", "Brand Storyteller"],
    ("creative", "strategic"): ["Product Strategist", "Innovation Lead", "GTM Specialist"],
    ("execution", "social"): ["Operations Manager", "Client Delivery Lead", "Sales Operations"],
    ("execution", "leadership"): ["Chief of Staff", "Operations Director", "Program Lead"],
    ("execution", "technical"): ["Engineering Manager", "DevOps Lead", "Technical PM"],
    ("leadership", "social"): ["People Operations Lead", "Head of Partnerships", "Community Director"],
    ("leadership", "strategic"): ["Business Unit Lead", "Chief of Staff", "Head of Strategy"],
    ("strategic", "communication"): ["Communications Director", "PR Strategist", "Thought Leadership Lead"],
    ("technical", "communication"): ["Developer Advocate", "Technical Writer", "Solutions Consultant"],
}

# ── Reasoning templates ──────────────────────────────────────────────────
# Written like a smart friend explaining, not a bot reporting scores.

_REASONING: dict[str, str] = {
    "analytical": (
        "You naturally lean toward solving complex problems and making structured decisions. "
        "Your answers consistently show a preference for evidence-based thinking "
        "and understanding systems before acting."
    ),
    "creative": (
        "You're drawn to building new things and thinking beyond conventional approaches. "
        "Your answers reveal someone who values originality, experimentation, "
        "and fresh perspectives over following the playbook."
    ),
    "execution": (
        "You're wired to make things happen. Your answers show someone who values output, "
        "efficiency, and shipping results over endless deliberation. "
        "You'd rather cut scope than miss a deadline."
    ),
    "social": (
        "You energize through people. Your answers consistently show that collaboration, "
        "influence, and human connection drive your best work. "
        "You're the person who keeps the team aligned."
    ),
    "leadership": (
        "You naturally step into the room and take charge. Your answers show someone "
        "who thinks about the people around them first — how to motivate, unblock, "
        "and bring out the best in a team."
    ),
    "strategic": (
        "You think two steps ahead. Your answers reveal a big-picture thinker "
        "who connects dots others miss — someone who asks 'why' before 'how' "
        "and spots opportunities before they're obvious."
    ),
    "technical": (
        "You're most at home building, coding, or deeply understanding how systems work. "
        "Your answers show a preference for precision, craft, and technical depth "
        "over abstract discussion."
    ),
    "communication": (
        "You think in stories and ideas. Your answers reveal someone who translates "
        "complex things clearly — whether that's writing, presenting, or making sure "
        "the message lands the way it was intended."
    ),
}

_CROSS_REASONING: dict[tuple[str, str], str] = {
    ("analytical", "creative"): (
        "You combine deep thinking with creative instinct — "
        "the kind of person who sees patterns others miss and turns them into new ideas."
    ),
    ("analytical", "execution"): (
        "You think clearly and ship consistently — "
        "a rare combination of rigorous analysis and bias toward action."
    ),
    ("analytical", "strategic"): (
        "You're a rigorous strategist — you don't just make plans, you stress-test them. "
        "That combination of analytical depth and big-picture thinking is rare."
    ),
    ("analytical", "technical"): (
        "You bring both depth and precision. "
        "You're equally comfortable modeling a problem and building the solution for it."
    ),
    ("creative", "execution"): (
        "You don't just have ideas — you build them. "
        "That maker mentality shows up clearly in both your answers and your background."
    ),
    ("creative", "communication"): (
        "You think in stories and bring them to life. "
        "Your combination of originality and clarity makes you a rare creative communicator."
    ),
    ("creative", "strategic"): (
        "You're the person in the room who sees what's possible before anyone else does — "
        "and can explain why it matters."
    ),
    ("execution", "social"): (
        "You get things done through people. "
        "Your blend of operational discipline and interpersonal skill makes you a natural team lead."
    ),
    ("execution", "leadership"): (
        "You lead by doing. Your answers reveal someone who earns trust through output — "
        "the kind of person teams follow because they deliver."
    ),
    ("leadership", "strategic"): (
        "You think at the org level. "
        "Your combination of people intuition and strategic clarity is what separates "
        "good managers from great ones."
    ),
    ("strategic", "communication"): (
        "You think big and communicate it clearly. "
        "That's rare — most people can do one but not both."
    ),
    ("technical", "communication"): (
        "You can build it and explain it. "
        "That bridge between technical depth and clear communication is one of the "
        "most in-demand combinations in tech."
    ),
}


def generate_result(profile: dict) -> dict:
    """Generate the final psychometric result from a fully scored profile.

    Returns a dict ready to store on the candidate and show in the UI.
    """
    scores = profile.get("scores") or {}
    sorted_dims = sorted(scores, key=lambda d: scores.get(d, 0), reverse=True)
    top_2 = sorted_dims[:2]

    # Build role recommendations
    roles = _build_roles(top_2, profile.get("resume_data") or {})

    # Build reasoning
    reasoning = _build_reasoning(top_2, profile)

    result = {
        "top_strengths": top_2,
        "dimension_scores": {d: round(scores.get(d, 0), 1) for d in sorted_dims},
        "traits": profile.get("traits") or [],
        "recommended_roles": roles,
        "reasoning": reasoning,
        "confidence_score": round(profile.get("confidence", 0), 1),
    }

    profile["result"] = result
    return result


def _build_roles(top_2: list[str], resume_data: dict) -> list[str]:
    """Build a blended role list from top dimensions + resume.

    Strategy:
    1. If top 2 dimensions have cross-specializations, lead with those
    2. Pull 2 roles from top dimension, 1 from second
    3. Blend in resume likely_roles if they exist (don't duplicate)
    4. Cap at 6
    """
    roles: list[str] = []

    # Cross-dimension specializations
    key = tuple(sorted(top_2))
    cross = _CROSS_ROLES.get(key) or _CROSS_ROLES.get(tuple(reversed(key))) or []
    roles.extend(cross[:2])

    # Primary dimension roles
    primary_roles = ROLE_MAP.get(top_2[0], [])
    for r in primary_roles:
        if r not in roles:
            roles.append(r)
            if len(roles) >= 4:
                break

    # Secondary dimension roles
    if len(top_2) > 1:
        secondary_roles = ROLE_MAP.get(top_2[1], [])
        for r in secondary_roles:
            if r not in roles:
                roles.append(r)
                if len(roles) >= 5:
                    break

    # Resume blend — add likely_roles that aren't already covered
    resume_roles = resume_data.get("likely_roles") or []
    for r in resume_roles[:2]:
        if r not in roles:
            roles.append(r)

    return roles[:6]


def _build_reasoning(top_2: list[str], profile: dict) -> str:
    """Generate human-readable reasoning about the candidate's profile."""
    parts: list[str] = []

    # Cross-dimension insight (if available)
    key = tuple(sorted(top_2))
    cross_text = _CROSS_REASONING.get(key) or _CROSS_REASONING.get(tuple(reversed(key)))
    if cross_text:
        parts.append(cross_text)
    else:
        # Primary dimension reasoning
        primary_text = _REASONING.get(top_2[0])
        if primary_text:
            parts.append(primary_text)

    # Resume alignment note
    resume = profile.get("resume_data") or {}
    domain = resume.get("domain")
    if domain:
        parts.append(f"This aligns with your background in {domain}.")

    # Trait summary
    traits = profile.get("traits") or []
    if traits and traits != ["Emerging Professional"]:
        trait_str = " and ".join(traits[:2]) if len(traits) <= 2 else ", ".join(traits[:2]) + f", and {traits[2]}"
        parts.append(f"We'd describe your working style as: {trait_str}.")

    # Confidence note
    confidence = profile.get("confidence", 0)
    if confidence >= 80:
        parts.append("Your answers were highly consistent — we're confident in this profile.")
    elif confidence < 50:
        parts.append(
            "Your answers showed range across dimensions — "
            "that's not a bad thing, it may mean you're genuinely versatile."
        )

    return " ".join(parts)
