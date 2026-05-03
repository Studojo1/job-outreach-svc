"""
Adaptive quiz engine — onboarding + progressive disclosure based on implicit clarity.
Zero LLM calls during quiz. Resume profile (pre-extracted after upload) powers adaptivity.

Always-on questions:
  Q1  career_stage      MCQ   always
  Q2  job_type          MCQ   skipped for experienced / career-switcher
  Q3  location          MCQ   options adapted to candidate's detected geography
  Q4  company_stage     MCQ   always
  Q5  career_goal       TEXT  always
  Q6  dream_companies   TEXT  adaptive examples based on Q4 company_stage
  Q7  target_role       MCQ   options from resume_profile.likely_roles if available
  Q8  work_mode         MCQ   always (drives organization_job_locations decision)

Progressive disclosure (gated on implicit _clarity_score, no extra question):
  Q9  niche_keywords    MCQ multi   medium + high clarity (drives q_organization_keyword_tags, OR'd up to 3)
  Q10 tech_stack        MCQ multi   high clarity AND technical cluster only (drives currently_using_any_of_technology_uids)

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
        "allow_multiple": True,
    },
    "text_input": False,
}

_Q5_CAREER_GOAL = {
    "key": "career_goal",
    "ack": "Good to know.",
    "message": "Describe what you'd actually be doing day-to-day in your ideal role. One sentence is fine. e.g. 'Running GTM strategy for an early-stage startup', 'Closing enterprise deals in SaaS', 'Analyzing data to improve a product'",
    "mcq": None,
    "text_input": True,
}

_Q8_WORK_MODE = {
    "key": "work_mode",
    "ack": "That helps a lot.",
    "message": "Last basic one — what work setup are you targeting?",
    "mcq": {
        "question": "What work setup are you targeting?",
        "options": [
            {"label": "A", "text": "Fully remote"},
            {"label": "B", "text": "Hybrid (mix of office + remote)"},
            {"label": "C", "text": "Fully in-office"},
            {"label": "D", "text": "Open to all"},
        ],
        "allow_multiple": False,
    },
    "text_input": False,
}

_Q_NICHE_KEYWORDS = {
    "key": "niche_keywords",
    "ack": "Great pick.",
    "message": "Which niches genuinely excite you? Pick up to 3 — we'll surface companies in these spaces.",
    "mcq": {
        "question": "Which niches do you want us to prioritize?",
        "options": [
            {"label": "A", "text": "Fintech / Payments"},
            {"label": "B", "text": "SaaS / B2B"},
            {"label": "C", "text": "Consumer apps / D2C"},
            {"label": "D", "text": "AI / ML"},
            {"label": "E", "text": "Edtech"},
            {"label": "F", "text": "Healthtech / Biotech"},
            {"label": "G", "text": "Devtools / Developer-first"},
            {"label": "H", "text": "Logistics / Supply chain"},
            {"label": "I", "text": "Climate / Sustainability"},
            {"label": "J", "text": "Gaming / Media"},
            {"label": "K", "text": "Agritech"},
            {"label": "L", "text": "No strong preference"},
        ],
        "allow_multiple": True,
    },
    "text_input": False,
}

# Tech-stack chip pickers per cluster — only fired for high-clarity technical candidates
_TECH_STACK_BY_CLUSTER: dict[str, list[str]] = {
    "software_engineering": [
        "Python", "JavaScript", "TypeScript", "Java", "Go", "React", "Node.js",
        "AWS", "Docker", "Kubernetes", "PostgreSQL", "MongoDB",
    ],
    "data_analytics": [
        "Python", "SQL", "R", "Tableau", "Power BI", "Snowflake", "BigQuery",
        "dbt", "Airflow", "Pandas", "Spark", "Looker",
    ],
    "data": [  # alias
        "Python", "SQL", "Tableau", "Power BI", "Snowflake", "BigQuery",
        "dbt", "Airflow", "Pandas", "Spark", "Looker", "R",
    ],
}


def _build_tech_stack_question(cluster_key: str) -> dict | None:
    chips = _TECH_STACK_BY_CLUSTER.get(cluster_key)
    if not chips:
        return None
    options = [{"label": chr(65 + i), "text": t} for i, t in enumerate(chips)]
    return {
        "key": "tech_stack",
        "ack": "Stack noted.",
        "message": "Pick up to 3 tools or technologies you most want to use day-to-day.",
        "mcq": {
            "question": "Top 3 tools/tech you want to use?",
            "options": options,
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
    "dream_companies": "Great!",
    "target_role": "Noted.",
    "work_mode": "Got it.",
    "niche_keywords": "Solid picks.",
    "tech_stack": "Stack noted.",
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _clarity_score(answers: dict, resume_profile: dict) -> int:
    """Implicit clarity-of-intent score derived from existing signals.

    No quiz question — derived from career_stage + resume strength + dream companies.
    Used to progressively disclose granular questions (niche keywords, tech stack)
    only to candidates who have enough clarity to answer them well.

    Returns int. ≥3 = high (show all granular Qs), 0-2 = medium, <0 = low.
    """
    score = 0
    cs = (answers.get("career_stage") or "").lower()
    if "experienced" in cs or "3+" in cs:
        score += 2
    elif "recent graduate" in cs or "0-2" in cs:
        score += 0
    elif "student" in cs:
        score -= 1
    elif "switching" in cs or "exploring" in cs:
        score -= 2

    likely = resume_profile.get("likely_roles") or []
    if len(likely) >= 3:
        score += 2
    elif len(likely) == 0:
        score -= 1

    skills = resume_profile.get("top_skills") or []
    if len(skills) >= 6:
        score += 1
    elif len(skills) <= 2:
        score -= 1

    raw_dream = (answers.get("dream_companies") or "").strip()
    if raw_dream and raw_dream.lower() != "skip":
        n = len([c for c in raw_dream.split(",") if c.strip()])
        if n >= 5:
            score += 2
        elif n == 0:
            score -= 2
    return score


def _detect_role_cluster(answers: dict, resume_profile: dict) -> str:
    """Best-guess role cluster used to gate the tech_stack chip question."""
    domain = (resume_profile.get("domain") or "").lower()
    if domain in ("software_engineering", "data_analytics", "data"):
        return domain
    target = (answers.get("target_role") or "").lower()
    if any(kw in target for kw in ("engineer", "developer", "backend", "frontend", "full stack", "ml", "devops", "sre")):
        return "software_engineering"
    if any(kw in target for kw in ("data ", "analyst", "analytics", "scientist", "bi ")):
        return "data_analytics"
    return ""


def build_question_sequence(state: dict) -> list[dict]:
    """
    Return the full ordered list of questions for this candidate.
    Questions are progressively disclosed based on an implicit clarity score
    derived from existing signals — no separate "how clear are you" prompt.
    """
    answers = state.get("answers", {})
    resume_profile = state.get("resume_profile") or {}
    resume_text = state.get("resume_text") or ""
    parsed_json = state.get("parsed_json") or {}

    stage = answers.get("career_stage", "").lower()
    skip_job_type = any(kw in stage for kw in ("experienced", "3+", "switching"))

    # ── Always-on onboarding ────────────────────────────────────────────
    sequence = [_Q1_CAREER_STAGE]
    if not skip_job_type:
        sequence.append(_Q2_JOB_TYPE)
    sequence.append(_build_location_question(resume_profile, resume_text, parsed_json))
    sequence.append(_Q4_COMPANY_STAGE)
    sequence.append(_Q5_CAREER_GOAL)
    sequence.append(_build_dream_companies_question(answers))
    sequence.append(_build_target_role_question(resume_profile, parsed_json))
    sequence.append(_Q8_WORK_MODE)

    # ── Progressive disclosure based on clarity ─────────────────────────
    clarity = _clarity_score(answers, resume_profile)

    if clarity >= 0:  # medium + high
        sequence.append(_Q_NICHE_KEYWORDS)

    if clarity >= 3:  # high only
        cluster = _detect_role_cluster(answers, resume_profile)
        ts_q = _build_tech_stack_question(cluster)
        if ts_q is not None:
            sequence.append(ts_q)

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


def _build_dream_companies_question(answers: dict) -> dict:
    """
    Build Q6 dream companies question — text input with adaptive examples
    based on Q4 company_stage answer.
    """
    stage = (answers.get("company_stage") or "").lower()

    if "early" in stage or "seed" in stage:
        examples = "Stripe, Zerodha, Notion"
    elif "growth" in stage:
        examples = "Razorpay, Swiggy, Meesho"
    elif "large" in stage or "mnc" in stage or "enterprise" in stage:
        examples = "EY, Google, McKinsey"
    else:
        examples = "Google, Deloitte, Flipkart"

    return {
        "key": "dream_companies",
        "ack": "Good to know.",
        "message": (
            f"Name a few companies you'd love to work at - even stretch goals count. "
            f"Separate with commas. e.g. '{examples}' (or type 'skip' if no preference)"
        ),
        "mcq": None,
        "text_input": True,
    }


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


def _detect_primary_domains(text: str) -> list[str]:
    """
    Frequency-weighted domain detection from resume text.
    Counts how many times each domain's keywords appear — returns top domains.
    Much more reliable than presence-only matching for noisy resume text.
    """
    # Each domain maps to (keywords, weight) — multi-word phrases score higher
    DOMAIN_KEYWORDS: dict[str, list[tuple[str, int]]] = {
        "marketing": [
            ("gtm", 3), ("go-to-market", 3), ("push-pull", 3), ("marketing strategy", 3),
            ("digital marketing", 2), ("content marketing", 2), ("performance marketing", 2),
            ("growth marketing", 2), ("seo", 2), ("paid ads", 2), ("campaigns", 2),
            ("brand", 1), ("marketing", 1), ("growth", 1),
        ],
        "consulting_strategy": [
            ("business generalist", 4), ("strategy consultant", 4), ("management consultant", 4),
            ("associate consultant", 4), ("strategy analyst", 3), ("end-to-end strategy", 3),
            ("bba", 3), ("mba", 3), ("stakeholder", 2), ("consulting", 2),
            ("strategic", 2), ("advisory", 2), ("strategy", 1), ("generalist", 1),
        ],
        "sales_bd": [
            ("business development", 3), ("revenue growth", 3), ("enterprise sales", 3),
            ("account executive", 3), ("deal", 2), ("sales strategy", 2),
            ("pipeline", 2), ("closing", 2), ("client acquisition", 2),
            ("sales", 1), ("revenue", 1), ("accounts", 1),
        ],
        "operations": [
            ("operations manager", 3), ("supply chain", 3), ("process improvement", 3),
            ("program manager", 3), ("project management", 3), ("scrum", 2),
            ("execution", 2), ("process", 1), ("operations", 1), ("logistics", 1),
        ],
        "finance": [
            ("investment banking", 4), ("private equity", 4), ("venture capital", 4),
            ("financial modelling", 4), ("financial modeling", 4), ("equity research", 3),
            ("mergers and acquisitions", 3), ("m&a", 3), ("cfa", 3), ("accounting", 2),
            ("valuation", 2), ("financial analysis", 2), ("finance", 1), ("investment", 1),
        ],
        "product_management": [
            ("product manager", 4), ("product management", 4), ("product roadmap", 4),
            ("product strategy", 3), ("product owner", 3), ("sprint planning", 3),
            ("user stories", 3), ("feature prioritization", 3), ("user research", 2),
            ("product", 1),
        ],
        "software_engineering": [
            ("software engineer", 4), ("full stack", 4), ("backend developer", 4),
            ("frontend developer", 4), ("software development", 3), ("devops", 3),
            ("python developer", 3), ("javascript", 2), ("react", 2), ("api development", 2),
            ("coding", 2), ("programming", 2), ("git", 1), ("developer", 1),
        ],
        "data_analytics": [
            ("data scientist", 4), ("data analyst", 4), ("machine learning engineer", 4),
            ("data science", 3), ("sql queries", 3), ("tableau", 3), ("power bi", 3),
            ("python for data", 3), ("statistical analysis", 3), ("analytics", 2),
            ("data analysis", 2), ("data-driven", 2), ("dashboard", 2), ("sql", 1),
        ],
        "design": [
            ("ux designer", 4), ("product designer", 4), ("ui/ux", 4),
            ("user experience design", 4), ("figma", 3), ("wireframe", 3),
            ("interaction design", 3), ("ux research", 2), ("prototyping", 2),
            ("user interface", 2), ("ux", 1), ("ui", 1),
        ],
        "hr_people": [
            ("talent acquisition", 4), ("human resources", 3), ("people operations", 3),
            ("technical recruiter", 3), ("hr manager", 3), ("employee engagement", 2),
            ("performance management", 2), ("recruiting", 2), ("hr", 1),
        ],
    }

    lower = text[:3000].lower()
    scores: dict[str, int] = {}
    for domain, keyword_weights in DOMAIN_KEYWORDS.items():
        total = sum(count * weight for kw, weight in keyword_weights
                    if (count := lower.count(kw)) > 0)
        if total > 0:
            scores[domain] = total

    # Return top domains sorted by score, minimum score threshold = 2
    ranked = [d for d, s in sorted(scores.items(), key=lambda x: -x[1]) if s >= 2]
    return ranked[:3] if ranked else []


def _build_target_role_question(resume_profile: dict, parsed_json: dict) -> dict:
    """
    Build Q6 target role question using career ontology for grounded, relevant options.

    Priority:
    1. resume_profile.likely_roles → match/enrich each against ontology
    2. resume_profile.domain + subdomain → pull ontology roles
    3. Frequency-weighted NLP from resume_text (via state passed down as parsed_json["_resume_text"])
    4. previously generated career_analysis.recommended_roles
    5. Absolute fallback (business-oriented, not tech)
    """
    from .career_ontology import CAREER_ONTOLOGY, search_ontology

    profile = resume_profile or {}
    likely_roles: list[str] = profile.get("likely_roles") or []
    domain = (profile.get("domain") or "").lower()
    # domain key → (ontology cluster, preferred spec keywords)
    DOMAIN_TO_CLUSTER: dict[str, tuple[str, list[str]]] = {
        "software_engineering": ("Technology & Engineering", ["software", "development"]),
        "data": ("Data & Analytics", ["data science", "analysis"]),
        "data_analytics": ("Data & Analytics", ["data analysis", "bi"]),
        "marketing": ("Marketing & Growth", ["growth", "digital", "content"]),
        "product": ("Consulting & Strategy", ["research", "strategy"]),
        "product_management": ("Consulting & Strategy", ["research", "strategy"]),
        "design": ("Design & Creative", ["ux", "visual"]),
        "finance": ("Finance & Accounting", ["investment", "fp&a"]),
        "sales": ("Sales & Business Development", ["inside sales", "account"]),
        "sales_bd": ("Sales & Business Development", ["business development", "inside sales"]),
        "operations": ("Operations & Supply Chain", ["business operations", "project"]),
        "hr_people": ("Human Resources & People Ops", ["talent", "generalist"]),
        "legal": ("Legal & Compliance", ["corporate", "regulatory"]),
        "consulting": ("Consulting & Strategy", ["management", "strategy"]),
        "consulting_strategy": ("Consulting & Strategy", ["management", "strategy"]),
        "healthcare": ("Healthcare & Life Sciences", ["clinical", "healthcare"]),
        "media": ("Media & Communications", ["digital media", "communications"]),
        "education": ("Education & Training", ["teaching", "edtech"]),
        "manufacturing": ("Manufacturing & Production", ["industrial", "production"]),
    }

    ontology_roles: list[str] = []

    def _pick_roles_from_cluster(cluster_name: str, spec_hints: list[str], limit: int = 3) -> list[str]:
        cluster = CAREER_ONTOLOGY.get(cluster_name, {})
        # Try to find specialization matching hints
        for spec_name, spec_roles in cluster.items():
            if any(h in spec_name.lower() for h in spec_hints):
                return [r for r in spec_roles[:limit] if r not in ontology_roles]
        # Fallback: first spec in cluster
        first = next(iter(cluster.values()), [])
        return [r for r in first[:limit] if r not in ontology_roles]

    # 1. LLM-extracted likely_roles → match against ontology
    if likely_roles:
        for role in likely_roles:
            results = search_ontology(role)
            if results["roles"]:
                ontology_roles.append(results["roles"][0]["role"])
            else:
                ontology_roles.append(role)

    # 2. resume_profile.domain → cluster roles
    if len(ontology_roles) < 4 and domain in DOMAIN_TO_CLUSTER:
        cluster_name, spec_hints = DOMAIN_TO_CLUSTER[domain]
        for r in _pick_roles_from_cluster(cluster_name, spec_hints, limit=3):
            ontology_roles.append(r)

    # 3. Frequency-weighted NLP from resume text (stored in state via parsed_json special key)
    if len(ontology_roles) < 3:
        resume_text = (parsed_json or {}).get("_resume_text") or ""
        if resume_text:
            detected = _detect_primary_domains(resume_text)
            for nlp_domain in detected[:2]:
                if nlp_domain in DOMAIN_TO_CLUSTER and len(ontology_roles) < 5:
                    cluster_name, spec_hints = DOMAIN_TO_CLUSTER[nlp_domain]
                    for r in _pick_roles_from_cluster(cluster_name, spec_hints, limit=2):
                        ontology_roles.append(r)

    # 4. previously generated career_analysis.recommended_roles
    if len(ontology_roles) < 3:
        career = (parsed_json or {}).get("career_analysis") or {}
        rec_roles = career.get("recommended_roles") or []
        for r in rec_roles:
            title = r.get("title") if isinstance(r, dict) else str(r)
            if title and title not in ontology_roles:
                ontology_roles.append(title)

    # 5. Business-oriented absolute fallback (not tech — covers BBA/MBA profiles)
    if not ontology_roles:
        ontology_roles = [
            "Strategy Analyst",
            "Growth Marketing Manager",
            "Sales Development Representative (SDR)",
            "Business Operations Associate",
            "Management Consultant (Analyst)",
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
