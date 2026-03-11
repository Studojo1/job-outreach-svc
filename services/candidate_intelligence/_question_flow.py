"""Question Flow Engine — extracted from the candidate intelligence main.py.

Provides ``get_active_questions`` and ``get_question`` without pulling in the
full FastAPI application from the intelligence layer.
"""

from services.candidate_intelligence.career_ontology import (
    CAREER_ONTOLOGY,
    get_all_clusters,
)

# ---------------------------------------------------------------------------
# Static question templates
# ---------------------------------------------------------------------------

STATIC_QUESTIONS = {
    "stage": {
        "ack": None,
        "message": "Which of these best describes you right now?",
        "mcq": {
            "question": "Which of these best describes you right now?",
            "options": [
                {"label": "A", "text": "I'm a student, not graduating soon"},
                {"label": "B", "text": "I'm a student, graduating within 6 months"},
                {"label": "C", "text": "Recent graduate (0-2 years experience)"},
                {"label": "D", "text": "Experienced professional (3+ years)"},
                {"label": "E", "text": "Switching careers or exploring new fields"},
                {"label": "F", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    "job_type": {
        "ack": "Got it!",
        "message": "Are you looking for an internship or a full-time role?",
        "mcq": {
            "question": "Are you looking for an internship or a full-time role?",
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
    },
    "location": {
        "ack": "Interesting!",
        "message": "Which cities or regions would you prefer to work in?",
        "mcq": {
            "question": "Which cities or regions would you prefer to work in?",
            "options": [
                {"label": "A", "text": "Bengaluru"},
                {"label": "B", "text": "Mumbai"},
                {"label": "C", "text": "Delhi NCR"},
                {"label": "D", "text": "Hyderabad"},
                {"label": "E", "text": "Pune"},
                {"label": "F", "text": "Chennai"},
                {"label": "G", "text": "Kolkata"},
                {"label": "H", "text": "Remote"},
                {"label": "I", "text": "International"},
                {"label": "J", "text": "Other"},
            ],
            "allow_multiple": True,
        },
        "text_input": False,
    },
    "work_style": {
        "ack": "Good choices.",
        "message": "What's your preferred work style?",
        "mcq": {
            "question": "What's your preferred work style?",
            "options": [
                {"label": "A", "text": "Fully remote"},
                {"label": "B", "text": "Hybrid (mix of office and remote)"},
                {"label": "C", "text": "Fully on-site / in office"},
                {"label": "D", "text": "Flexible, no strong preference"},
                {"label": "E", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    "company_stage": {
        "ack": "Noted.",
        "message": "What type of company appeals to you the most?",
        "mcq": {
            "question": "What type of company appeals to you?",
            "options": [
                {"label": "A", "text": "Early-stage startup (seed / under 50 people)"},
                {"label": "B", "text": "Growth-stage startup (50-500 people)"},
                {"label": "C", "text": "Mid-size company (500-2000)"},
                {"label": "D", "text": "Large enterprise or MNC (2000+)"},
                {"label": "E", "text": "Government / Public sector"},
                {"label": "F", "text": "No preference"},
                {"label": "G", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    "industry": {
        "ack": "Makes sense.",
        "message": "Which industries excite you the most?",
        "mcq": {
            "question": "Which industries excite you the most?",
            "options": [
                {"label": "A", "text": "Fintech / Payments / Banking"},
                {"label": "B", "text": "Edtech / Education"},
                {"label": "C", "text": "Healthcare / Healthtech"},
                {"label": "D", "text": "E-commerce / D2C / Retail"},
                {"label": "E", "text": "SaaS / Enterprise Software"},
                {"label": "F", "text": "AI / Machine Learning / Deep Tech"},
                {"label": "G", "text": "Media / Content / Gaming"},
                {"label": "H", "text": "Consulting / Professional Services"},
                {"label": "I", "text": "Manufacturing / Automotive"},
                {"label": "J", "text": "Other"},
            ],
            "allow_multiple": True,
        },
        "text_input": False,
    },
    "salary": {
        "ack": "Good to know.",
        "message": "What's your expected annual salary or CTC range? (e.g. ₹4-6 LPA, ₹15-20 LPA)",
        "mcq": None,
        "text_input": True,
    },
    "role_focus": {
        "ack": "Thanks, noted.",
        "message": "What kind of day-to-day work do you enjoy the most?",
        "mcq": {
            "question": "What kind of day-to-day work do you enjoy most?",
            "options": [
                {"label": "A", "text": "Building products and writing code"},
                {"label": "B", "text": "Analyzing data and finding insights"},
                {"label": "C", "text": "Designing user experiences and visuals"},
                {"label": "D", "text": "Marketing, growth, and customer acquisition"},
                {"label": "E", "text": "Strategy, planning, and business development"},
                {"label": "F", "text": "Managing teams and stakeholders"},
                {"label": "G", "text": "Research, writing, and content creation"},
                {"label": "H", "text": "Other"},
            ],
            "allow_multiple": True,
        },
        "text_input": False,
    },
    "skills": {
        "ack": "Great choice!",
        "message": "Which skills do you want to actively use or develop in your next role?",
        "mcq": {
            "question": "Which skills do you want to use or grow?",
            "options": [
                {"label": "A", "text": "Python / JavaScript / Programming"},
                {"label": "B", "text": "Data analysis, SQL, Excel"},
                {"label": "C", "text": "Product management / Roadmapping"},
                {"label": "D", "text": "UI/UX Design / Figma"},
                {"label": "E", "text": "Digital marketing / SEO / Ads"},
                {"label": "F", "text": "Communication and public speaking"},
                {"label": "G", "text": "Leadership and people management"},
                {"label": "H", "text": "Machine learning / AI / Deep learning"},
                {"label": "I", "text": "Financial analysis / Modeling"},
                {"label": "J", "text": "Other"},
            ],
            "allow_multiple": True,
        },
        "text_input": False,
    },
    "timeline": {
        "ack": "Got it!",
        "message": "When are you looking to start your next role?",
        "mcq": {
            "question": "When do you want to start?",
            "options": [
                {"label": "A", "text": "Immediately (within 1 month)"},
                {"label": "B", "text": "In 1-3 months"},
                {"label": "C", "text": "In 3-6 months"},
                {"label": "D", "text": "6+ months (just exploring)"},
                {"label": "E", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    "education_level": {
        "ack": "Understood.",
        "message": "What is your highest level of education?",
        "mcq": {
            "question": "What is your highest level of education?",
            "options": [
                {"label": "A", "text": "High School / Diploma"},
                {"label": "B", "text": "Bachelor's Degree"},
                {"label": "C", "text": "Master's Degree"},
                {"label": "D", "text": "PhD / Doctorate"},
                {"label": "E", "text": "Self-taught / Bootcamp"},
                {"label": "F", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    "years_experience": {
        "ack": "Got it.",
        "message": "Roughly how many years of total professional experience do you have?",
        "mcq": {
            "question": "Years of experience?",
            "options": [
                {"label": "A", "text": "0-1 years (Entry level)"},
                {"label": "B", "text": "1-3 years (Junior)"},
                {"label": "C", "text": "3-5 years (Mid-level)"},
                {"label": "D", "text": "5-10 years (Senior)"},
                {"label": "E", "text": "10+ years (Staff / Lead)"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
}


def _build_domain_question() -> dict:
    clusters = get_all_clusters()
    options = [{"label": chr(65 + i), "text": c} for i, c in enumerate(clusters[:12])]
    options.append({"label": chr(65 + len(options)), "text": "Other"})
    return {
        "ack": "Nice.",
        "message": "What broad career domain excites you the most? Pick 1-3.",
        "mcq": {
            "question": "What broad career domain excites you the most?",
            "options": options,
            "allow_multiple": True,
        },
        "text_input": False,
    }


def _build_specialization_question(selected_domains: list[str]) -> dict:
    specs = []
    for domain in selected_domains:
        for cluster_name, specializations in CAREER_ONTOLOGY.items():
            if domain.lower() in cluster_name.lower() or cluster_name.lower() in domain.lower():
                specs.extend(list(specializations.keys()))

    seen = set()
    unique_specs = []
    for s in specs:
        if s not in seen:
            seen.add(s)
            unique_specs.append(s)
    unique_specs = unique_specs[:10]

    if not unique_specs:
        for _, specializations in list(CAREER_ONTOLOGY.items())[:5]:
            unique_specs.extend(list(specializations.keys())[:2])
        unique_specs = unique_specs[:10]

    options = [{"label": chr(65 + i), "text": spec} for i, spec in enumerate(unique_specs)]
    options.append({"label": chr(65 + len(options)), "text": "Other"})

    domain_names = ", ".join(selected_domains[:2])
    return {
        "ack": "Great choices!",
        "message": f"Within {domain_names}, which specializations interest you?",
        "mcq": {
            "question": f"Which specialization in {domain_names} interests you most?",
            "options": options,
            "allow_multiple": True,
        },
        "text_input": False,
    }


def get_question(q_id: str, session: dict) -> dict:
    if q_id == "domain":
        return _build_domain_question()
    elif q_id == "specialization":
        domains = session.get("answers", {}).get("domain", [])
        if isinstance(domains, str):
            domains = [domains]
        return _build_specialization_question(domains)
    else:
        return STATIC_QUESTIONS.get(q_id, STATIC_QUESTIONS["stage"])


def get_active_questions(session: dict) -> list[str]:
    questions = [
        "stage", "job_type", "domain", "specialization", "location",
        "work_style", "company_stage", "industry", "salary",
        "role_focus", "skills", "timeline",
    ]

    answers = session.get("answers", {})

    def _to_str(val) -> str:
        if isinstance(val, list):
            return ", ".join(val)
        return str(val) if val else ""

    stage = _to_str(answers.get("stage")).lower()
    job_type = _to_str(answers.get("job_type")).lower()

    has_resume = session.get("resume_uploaded", False) or bool(session.get("resume_raw_text"))
    if not has_resume:
        questions.insert(1, "education_level")
        questions.insert(2, "years_experience")

    if "student" in stage and "not graduating" in stage:
        for q in ["job_type", "salary", "years_experience"]:
            if q in questions:
                questions.remove(q)
    elif "intern" in job_type:
        for q in ["salary", "years_experience"]:
            if q in questions:
                questions.remove(q)
    elif "experienced" in stage or "3+" in stage:
        if "job_type" in questions:
            questions.remove("job_type")

    if has_resume:
        resume_skills = (session.get("resume_summary") or {}).get("skills", [])
        if len(resume_skills) >= 3 and "skills" in questions:
            questions.remove("skills")

    return questions
