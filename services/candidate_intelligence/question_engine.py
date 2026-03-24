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
from .city_data import (
    get_cities_for_country,
    detect_country_from_text,
    detect_country_from_parsed_json,
)

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
    parsed_json = state.get("parsed_json") or {}

    stage = answers.get("career_stage", "").lower()
    skip_job_type = any(kw in stage for kw in ("experienced", "3+", "switching"))

    sequence = [_Q1_CAREER_STAGE]
    if not skip_job_type:
        sequence.append(_Q2_JOB_TYPE)
    sequence.append(_build_location_question(resume_profile, resume_text, parsed_json))
    sequence.append(_Q4_COMPANY_STAGE)
    sequence.append(_Q5_CAREER_GOAL)
    sequence.append(_build_target_role_question(resume_profile, parsed_json))
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

def _build_location_question(resume_profile: dict, resume_text: str, parsed_json: dict) -> dict:
    """
    Build Q3 location question with city options tailored to candidate's geography.

    Detection priority:
    1. resume_profile.geography.country_code  (LLM-extracted, most accurate)
    2. parsed_json personal_info fields        (structured parser output)
    3. detect_country_from_text()              (header + phone code patterns only)
    4. Default to "IN"
    """
    country_code = None

    # 1. resume_profile from background LLM extraction
    geo = (resume_profile or {}).get("geography") or {}
    if geo.get("country_code"):
        country_code = geo["country_code"]

    # 2. parsed_json personal_info (structured data from parser)
    if not country_code:
        country_code = detect_country_from_parsed_json(parsed_json)

    # 3. Smart text heuristics (location labels, phone codes — NOT bare city mentions)
    if not country_code:
        country_code = detect_country_from_text(resume_text)

    # 4. Default
    if not country_code:
        country_code = "IN"

    cities = get_cities_for_country(country_code, limit=6)

    if not cities:
        # Unknown country — show global hubs
        raw_cities = ["Remote", "Open to relocate / International", "Other"]
    else:
        raw_cities = list(cities) + ["Remote", "Open to relocate / International", "Other"]

    options = [{"label": chr(65 + i), "text": city} for i, city in enumerate(raw_cities)]

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


def _build_target_role_question(resume_profile: dict, parsed_json: dict) -> dict:
    """
    Build Q6 target role question using career ontology for grounded, relevant options.

    Priority:
    1. resume_profile.likely_roles → match/enrich each against ontology
    2. resume_profile.domain + subdomain → pull ontology roles for those clusters
    3. parsed_json.career_analysis.recommended_roles (previously generated profile)
    4. Broad cross-domain defaults
    """
    from .career_ontology import CAREER_ONTOLOGY, search_ontology

    profile = resume_profile or {}
    likely_roles: list[str] = profile.get("likely_roles") or []
    domain = (profile.get("domain") or "").lower()
    subdomain = (profile.get("subdomain") or "").lower()

    # --- domain → ontology cluster mapping ---
    DOMAIN_TO_CLUSTER = {
        "software_engineering": "Technology & Engineering",
        "data": "Data & Analytics",
        "marketing": "Marketing & Growth",
        "product": "Consulting & Strategy",  # PM sits between tech/consulting
        "design": "Design & Creative",
        "finance": "Finance & Accounting",
        "sales": "Sales & Business Development",
        "operations": "Operations & Supply Chain",
        "hr_people": "Human Resources & People Ops",
        "legal": "Legal & Compliance",
        "consulting": "Consulting & Strategy",
        "healthcare": "Healthcare & Life Sciences",
        "media": "Media & Communications",
        "education": "Education & Training",
        "manufacturing": "Manufacturing & Production",
    }

    ontology_roles: list[str] = []

    # 1. Map likely_roles through ontology (exact + substring match)
    if likely_roles:
        for role in likely_roles:
            results = search_ontology(role)
            # If exact match found in ontology, prefer it
            if results["roles"]:
                ontology_roles.append(results["roles"][0]["role"])
            else:
                # Use the LLM role as-is if no ontology match (still better than nothing)
                ontology_roles.append(role)

    # 2. Fill from domain cluster if we don't have enough
    if len(ontology_roles) < 4 and domain in DOMAIN_TO_CLUSTER:
        cluster_name = DOMAIN_TO_CLUSTER[domain]
        cluster = CAREER_ONTOLOGY.get(cluster_name, {})
        # Pick the most relevant specialization based on subdomain
        best_spec_roles: list[str] = []
        for spec_name, spec_roles in cluster.items():
            if subdomain and any(w in spec_name.lower() for w in subdomain.split("_")):
                best_spec_roles = spec_roles[:3]
                break
        if not best_spec_roles:
            # First specialization in the cluster
            first_spec = next(iter(cluster.values()), [])
            best_spec_roles = first_spec[:3]
        for r in best_spec_roles:
            if r not in ontology_roles:
                ontology_roles.append(r)

    # 3. Fallback: previously generated recommended_roles from career_analysis
    if len(ontology_roles) < 3:
        career = (parsed_json or {}).get("career_analysis") or {}
        rec_roles = career.get("recommended_roles") or []
        for r in rec_roles:
            title = r.get("title") if isinstance(r, dict) else str(r)
            if title and title not in ontology_roles:
                ontology_roles.append(title)

    # 4. Absolute fallback
    if not ontology_roles:
        ontology_roles = [
            "Software Engineer", "Data Analyst", "Product Manager",
            "Business Analyst", "Operations Analyst",
        ]

    # Deduplicate, cap at 5, add "Something else"
    seen, final_roles = set(), []
    for r in ontology_roles:
        if r not in seen:
            seen.add(r)
            final_roles.append(r)
        if len(final_roles) == 5:
            break

    options = [{"label": chr(65 + i), "text": r} for i, r in enumerate(final_roles)]
    options.append({"label": chr(65 + len(final_roles)), "text": "Something else"})

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
