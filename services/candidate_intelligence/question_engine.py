"""
Adaptive quiz question engine — all 7 questions served deterministically.
Zero LLM calls during quiz. Resume profile (pre-extracted after upload) powers
Q6 adaptivity.

Question sequence:
  Q1  career_stage      MCQ   always
  Q2  job_type          MCQ   skipped for experienced / career-switcher
  Q3  location          MCQ   options adapted to candidate's detected geography
  Q4  company_stage     MCQ   always
  Q5  career_goal       TEXT  always
  Q6  target_role       MCQ   options from resume_profile.likely_roles if available
  Q7  work_motivation   MCQ   always

State passed to get_next_question():
  {
    "answers": {"career_stage": "...", "job_type": "...", ...},  # keyed by q_key
    "resume_profile": {...} | None,   # from candidates.resume_profile
    "resume_text": str | None,        # for city NLP fallback
  }
"""

from __future__ import annotations
from .city_data import get_cities_for_country, detect_country_from_text

# ---------------------------------------------------------------------------
# Static question definitions
# ---------------------------------------------------------------------------

_Q1_CAREER_STAGE = {
    "key": "career_stage",
    "ack": None,
    "message": "Let's map out your career goals! Which of these best describes where you are right now?",
    "mcq": {
        "question": "Which best describes you right now?",
        "options": [
            {"label": "A", "text": "Student, not graduating soon"},
            {"label": "B", "text": "Student, graduating within 6 months"},
            {"label": "C", "text": "Recent graduate (0-2 years exp.)"},
            {"label": "D", "text": "Experienced professional (3+ years)"},
            {"label": "E", "text": "Switching careers / exploring new fields"},
            {"label": "F", "text": "Other"},
        ],
        "allow_multiple": False,
    },
    "text_input": False,
}

_Q2_JOB_TYPE = {
    "key": "job_type",
    "ack": "Got it!",
    "message": "Are you targeting an internship or a full-time role?",
    "mcq": {
        "question": "What type of opportunity are you looking for?",
        "options": [
            {"label": "A", "text": "Full-time job"},
            {"label": "B", "text": "Internship (3-6 months)"},
            {"label": "C", "text": "Part-time or freelance"},
            {"label": "D", "text": "Open to all options"},
            {"label": "E", "text": "Other"},
        ],
        "allow_multiple": False,
    },
    "text_input": False,
}

_Q4_COMPANY_STAGE = {
    "key": "company_stage",
    "ack": "Makes sense.",
    "message": "What kind of company environment appeals to you most?",
    "mcq": {
        "question": "Company type?",
        "options": [
            {"label": "A", "text": "Early-stage startup (seed, under 50 people)"},
            {"label": "B", "text": "Growth-stage startup (50-500 people)"},
            {"label": "C", "text": "Mid-size company (500-2000)"},
            {"label": "D", "text": "Large enterprise or MNC (2000+)"},
            {"label": "E", "text": "No strong preference"},
            {"label": "F", "text": "Other"},
        ],
        "allow_multiple": False,
    },
    "text_input": False,
}

_Q5_CAREER_GOAL = {
    "key": "career_goal",
    "ack": "Good to know.",
    "message": "What's the one thing you really want from your next role? e.g. 'Learn to close enterprise deals', 'Build something people actually use', 'Hit 15-20 LPA fast'",
    "mcq": None,
    "text_input": True,
}

_Q7_WORK_MOTIVATION = {
    "key": "work_motivation",
    "ack": "That helps a lot.",
    "message": "Last one — what drives you most at work right now?",
    "mcq": {
        "question": "What drives you most at work?",
        "options": [
            {"label": "A", "text": "Learning fast and building new skills"},
            {"label": "B", "text": "Making a real impact on the product"},
            {"label": "C", "text": "Strong compensation and career growth"},
            {"label": "D", "text": "Being part of a high-energy team"},
            {"label": "E", "text": "Working on something with a clear mission"},
            {"label": "F", "text": "Autonomy to work independently"},
        ],
        "allow_multiple": True,
    },
    "text_input": False,
}

# ---------------------------------------------------------------------------
# Ack messages keyed by question key — shown before next question
# ---------------------------------------------------------------------------
_ACKS: dict[str, str] = {
    "career_stage": "Got it!",
    "job_type": "Got it!",
    "location": "Makes sense.",
    "company_stage": "Good to know.",
    "career_goal": "That helps a lot.",
    "target_role": "Noted.",
    "work_motivation": None,  # last question, no ack needed
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_question_sequence(state: dict) -> list[dict]:
    """
    Return the full ordered list of questions for this candidate.
    Some questions are skipped or have dynamic options based on state.
    """
    answers = state.get("answers", {})
    resume_profile = state.get("resume_profile") or {}
    resume_text = state.get("resume_text") or ""

    stage = answers.get("career_stage", "").lower()
    skip_job_type = any(kw in stage for kw in ("experienced", "3+", "switching"))

    sequence = [_Q1_CAREER_STAGE]
    if not skip_job_type:
        sequence.append(_Q2_JOB_TYPE)
    sequence.append(_build_location_question(resume_profile, resume_text))
    sequence.append(_Q4_COMPANY_STAGE)
    sequence.append(_Q5_CAREER_GOAL)
    sequence.append(_build_target_role_question(resume_profile))
    sequence.append(_Q7_WORK_MOTIVATION)

    return sequence


def get_next_question(state: dict) -> dict | None:
    """
    Return the next question dict, or None if the quiz is complete.
    State must include: answers (dict keyed by q_key), resume_profile, resume_text.
    """
    sequence = build_question_sequence(state)
    answered_keys = set(state.get("answers", {}).keys())

    for q in sequence:
        if q["key"] not in answered_keys:
            return q

    return None  # all answered → complete


def get_question_at_index(state: dict, index: int) -> dict | None:
    """Return the question at a specific 0-based index in the sequence."""
    sequence = build_question_sequence(state)
    if index < len(sequence):
        return sequence[index]
    return None


def count_total_questions(state: dict) -> int:
    """Return total question count for this candidate's sequence."""
    return len(build_question_sequence(state))


def build_message(q_def: dict, prev_q_key: str | None, is_first: bool) -> str:
    """
    Build the SSE message text: ack for previous answer + this question.
    """
    if is_first or not prev_q_key:
        return q_def["message"]
    ack = _ACKS.get(prev_q_key) or "Got it."
    return f"{ack}|||{q_def['message']}"


# ---------------------------------------------------------------------------
# Adaptive question builders
# ---------------------------------------------------------------------------

def _build_location_question(resume_profile: dict, resume_text: str) -> dict:
    """
    Build Q3 location question with city options tailored to candidate's geography.
    Falls back to India cities if no geography detected.
    """
    # Try resume_profile.geography first, then scan resume text
    geo = resume_profile.get("geography") or {}
    country_code = geo.get("country_code") or detect_country_from_text(resume_text) or "IN"

    cities = get_cities_for_country(country_code, limit=6)

    if not cities:
        # Absolute fallback — global options
        options = [
            {"label": "A", "text": "Bengaluru"},
            {"label": "B", "text": "Mumbai"},
            {"label": "C", "text": "Delhi NCR"},
            {"label": "D", "text": "Remote"},
            {"label": "E", "text": "Open to relocate / International"},
            {"label": "F", "text": "Other"},
        ]
    else:
        raw_options = list(cities) + ["Remote", "Open to relocate / International", "Other"]
        options = [{"label": chr(65 + i), "text": city} for i, city in enumerate(raw_options)]

    return {
        "key": "location",
        "ack": "Got it!",
        "message": "Which cities or regions would you prefer to work in?",
        "mcq": {
            "question": "Preferred work locations?",
            "options": options,
            "allow_multiple": True,
        },
        "text_input": False,
    }


def _build_target_role_question(resume_profile: dict) -> dict:
    """
    Build Q6 target role question.
    If resume_profile has likely_roles, use those as MCQ options.
    Otherwise use a domain-based default list or ask as open text.
    """
    likely_roles: list[str] = resume_profile.get("likely_roles") or []
    domain = (resume_profile.get("domain") or "").lower()

    if likely_roles:
        # Use LLM-extracted roles + "Other" option
        roles = likely_roles[:5]
        options = [{"label": chr(65 + i), "text": r} for i, r in enumerate(roles)]
        options.append({"label": chr(65 + len(roles)), "text": "Something else (tell us below)"})
        return {
            "key": "target_role",
            "ack": "That helps a lot.",
            "message": "Based on your background, which of these roles are you aiming for?",
            "mcq": {
                "question": "Which role fits best?",
                "options": options,
                "allow_multiple": True,
            },
            "text_input": False,
        }

    # Domain-based fallback options
    domain_options: dict[str, list[str]] = {
        "software_engineering": [
            "Software Engineer (Backend)", "Software Engineer (Frontend)",
            "Full Stack Developer", "DevOps / SRE", "Mobile Developer", "Other",
        ],
        "data": [
            "Data Analyst", "Data Scientist", "ML Engineer",
            "Business Analyst", "Data Engineer", "Other",
        ],
        "marketing": [
            "Growth Marketer", "Content Marketer", "Performance Marketer",
            "Brand Manager", "Product Marketer", "Other",
        ],
        "product": [
            "Product Manager", "Associate PM", "Product Analyst",
            "Technical PM", "Product Strategy", "Other",
        ],
        "design": [
            "Product Designer (UX/UI)", "Visual Designer",
            "UX Researcher", "Brand Designer", "Motion Designer", "Other",
        ],
        "finance": [
            "Investment Banking Analyst", "Financial Analyst",
            "Equity Research", "VC / PE Analyst", "Corporate Finance", "Other",
        ],
        "sales": [
            "Sales Development Rep (SDR)", "Account Executive",
            "Business Development", "Enterprise Sales", "Partnerships", "Other",
        ],
        "operations": [
            "Operations Manager", "Program Manager", "Management Consultant",
            "Business Analyst", "Strategy & Ops", "Other",
        ],
    }

    role_list = domain_options.get(domain, [
        "Software Engineer", "Product Manager", "Marketing Specialist",
        "Business Analyst", "Operations", "Other",
    ])

    options = [{"label": chr(65 + i), "text": r} for i, r in enumerate(role_list)]

    return {
        "key": "target_role",
        "ack": "That helps a lot.",
        "message": "Which of these roles are you targeting?",
        "mcq": {
            "question": "Target role?",
            "options": options,
            "allow_multiple": True,
        },
        "text_input": False,
    }
