"""Email Generator Service — AI-powered, human-sounding job seeker outreach.

Generates emails that sound like a thoughtful job seeker reaching out to
someone they respect — NOT a recruiter, NOT a sales pitch.

Each email is tailored using:
- Lead data: name, title, company, industry
- Candidate profile: name, skills, target roles, background
- Style: warm_intro, value_prop, company_curiosity, peer_to_peer, direct_ask
"""

from typing import Tuple, Dict, Any
from job_outreach_tool.database.models import Lead, Candidate
from job_outreach_tool.services.shared.ai.azure_openai_client import generate_json
from job_outreach_tool.core.logger import get_logger

logger = get_logger(__name__)

# Email style descriptions
STYLE_DESCRIPTIONS = {
    "warm_intro": {
        "tone": "Warm, genuine, humble",
        "approach": "Lead with a sincere observation about their work, then introduce yourself briefly",
    },
    "value_prop": {
        "tone": "Professional, specific, grounded",
        "approach": "Mention a specific skill or project that's relevant to their team's work",
    },
    "company_curiosity": {
        "tone": "Genuinely curious, enthusiastic but restrained",
        "approach": "Reference something specific the company is building that excites you",
    },
    "peer_to_peer": {
        "tone": "Casual, collegial, relatable",
        "approach": "Connect on shared technical interests or challenges",
    },
    "direct_ask": {
        "tone": "Concise, respectful, no fluff",
        "approach": "State who you are, why you're reaching out, and a clear ask — all in under 5 sentences",
    },
}


def assign_style(lead: Lead, selected_styles: list) -> str:
    """Assign the best email style to a lead based on their title and company size."""
    if not selected_styles:
        return "warm_intro"
    if len(selected_styles) == 1:
        return selected_styles[0]

    scores: Dict[str, int] = {}
    title_lower = (lead.title or "").lower()

    for style in selected_styles:
        score = 0
        if style == "warm_intro":
            if lead.company_size in ["1-10", "11-50"]:
                score += 3
            if any(t in title_lower for t in ["founder", "ceo", "head of", "owner"]):
                score += 2
        elif style == "value_prop":
            if lead.company_size in ["51-200", "201-1000", "1001-5000"]:
                score += 3
            if any(t in title_lower for t in ["manager", "director", "head", "lead"]):
                score += 2
        elif style == "company_curiosity":
            if lead.company_size in ["11-50", "51-200"]:
                score += 3
        elif style == "peer_to_peer":
            if any(t in title_lower for t in ["engineer", "analyst", "developer", "designer"]):
                score += 3
        elif style == "direct_ask":
            if any(t in title_lower for t in ["vp", "director", "cto", "chief"]):
                score += 3
        scores[style] = score

    best_style = max(scores, key=scores.get)
    logger.info("[EmailGen] Style '%s' for %s at %s (scores: %s)", best_style, lead.name, lead.company, scores)
    return best_style


def _extract_candidate_context(candidate: Candidate) -> dict:
    """Extract candidate info from parsed_json, handling the nested payload structure."""
    parsed = candidate.parsed_json or {}

    # The payload has nested structure: personal_info, preferences, career_analysis
    personal = parsed.get("personal_info", {})
    career = parsed.get("career_analysis", {})
    prefs = parsed.get("preferences", {})

    # Name: try personal_info.name, then top-level, then fallback
    name = personal.get("name") or parsed.get("name") or ""

    # Skills: from personal_info.skills_detected or top-level
    skills = personal.get("skills_detected", []) or parsed.get("skills", [])
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",")]

    # Target roles from career_analysis.recommended_roles
    recommended = career.get("recommended_roles", [])
    target_roles = [r.get("title", "") for r in recommended if r.get("title")]
    if not target_roles:
        target_roles = parsed.get("target_roles", []) or candidate.target_roles or []

    # Background: profile_summary or build from career analysis
    summary = parsed.get("profile_summary", "")

    # Location preferences
    locations = prefs.get("locations", [])

    return {
        "name": name,
        "skills": skills[:8],
        "target_roles": target_roles[:3],
        "summary": summary,
        "locations": locations,
    }


def generate_email_for_lead(lead: Lead, candidate: Candidate, style: str) -> Tuple[str, str]:
    """Generate a human-sounding job seeker outreach email.

    The email reads like a thoughtful person reaching out to someone they respect —
    NOT a recruiter message, NOT a sales pitch, NOT a referral request.

    Returns:
        Tuple of (subject, body) strings.
    """
    ctx = _extract_candidate_context(candidate)
    style_info = STYLE_DESCRIPTIONS.get(style, STYLE_DESCRIPTIONS["warm_intro"])

    # Randomly pick a variation seed to avoid identical structures
    import random
    variation = random.choice(["A", "B", "C"])

    prompt = f"""Write a short cold email from {ctx['name']} to {lead.name}.

Context:
- {ctx['name']} is looking for a {', '.join(ctx['target_roles'][:2]) if ctx['target_roles'] else 'software engineering'} role
- {ctx['name']}'s skills: {', '.join(ctx['skills'][:5]) if ctx['skills'] else 'software development'}
- {ctx['summary'][:150] if ctx['summary'] else 'Early-career professional'}
- {lead.name} is {lead.title} at {lead.company} ({lead.industry or 'tech'})
- Style: {style_info['tone']}

Write it like a real person typed this in 2 minutes. Not a cover letter. Not a LinkedIn message. Just a quick, genuine email.

IMPORTANT RULES:
- Total body: 60-90 words max. Short sentences. 2-3 small paragraphs.
- Start with "Hi {lead.name}," (not "Dear")
- First line: something specific about {lead.company} that caught their eye. Keep it simple — "I noticed your team has been..." or "I saw that {lead.company} is..."
- Middle: one or two sentences about who {ctx['name']} is and what they're looking for. No laundry list of skills.
- End: a casual ask — "Would you mind pointing me in the right direction?" or "Happy to keep it to a quick chat if you're open to it."
- Sign off: "Best,\\n{ctx['name']}" or "Cheers,\\n{ctx['name']}"
- Subject: lowercase-ish, casual, under 45 chars. Like texting a subject line. Examples: "quick question about eng at {lead.company}", "curious about {lead.company}"

AVOID these — they make it sound AI-generated:
- "I admire your organization's commitment to..."
- "I would be honored to contribute..."
- "I came across your impressive work..."
- "I was really impressed by the scale of problems..."
- Any sentence starting with "As a..." or "With my experience in..."
- Bullet points or numbered lists in the body
- More than 3 paragraphs

Variation seed: {variation} (use this to slightly vary your phrasing — different opening, different closing ask, different sentence rhythm)

Return valid JSON only:
{{"subject": "...", "body": "..."}}

Body must use \\n\\n between paragraphs.
"""

    schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "maxLength": 100},
            "body": {"type": "string", "maxLength": 2000},
        },
        "required": ["subject", "body"],
    }

    try:
        logger.info("[EmailGen] Generating for %s (%s) at %s, style=%s",
                    lead.name, lead.title, lead.company, style)

        result = generate_json(prompt, schema, temperature=0.85)
        subject = result.get("subject", "").strip()
        body = result.get("body", "").strip()

        if not subject or len(subject) < 5:
            raise ValueError("Subject too short")
        if not body or len(body) < 30:
            raise ValueError("Body too short")

        logger.info("[EmailGen] Generated for %s: subject='%s'", lead.name, subject[:50])
        return subject, body

    except Exception as e:
        logger.error("[EmailGen] Failed for %s: %s", lead.name, e, exc_info=True)
        raise ValueError(f"Email generation failed for {lead.name}: {e}")