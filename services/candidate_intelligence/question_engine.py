"""
Adaptive quiz engine — onboarding with explicit clarity branching.
Zero LLM calls during quiz. Resume profile (pre-extracted after upload) powers adaptivity.

Always-on questions:
  Q1  career_stage         MCQ   always
  Q2  clarity              MCQ   always — user explicitly tells us how clear they are
  Q3  job_type             MCQ   skipped for experienced / career-switcher
  Q4  location             MCQ   options adapted to candidate's detected geography
  Q5  company_stage        MCQ   always
  Q6  career_goal          TEXT  always
  Q7  dream_companies      TEXT  adaptive examples based on Q5 company_stage
  Q8  target_role          MCQ   options from resume_profile.likely_roles if available
  Q9  work_mode            MCQ   always (drives organization_job_locations decision)

Clarity-gated extras (asked based on the user's own clarity answer):
  Q10 niche_keywords       MCQ multi   asked unless clarity == "still figuring out"
  Q11 tech_stack           MCQ multi   asked when clarity == "exact" AND cluster is technical

Email-personalization questions (always asked at the end of the quiz, role-adaptive):
  Q12 flex_best_project    TEXT  always  → persisted to candidate.flex_notes
  Q13 flex_outcome         TEXT  always  → persisted to candidate.flex_notes

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
    "message": "Let's figure out the right companies for you. First up, where are you right now?",
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

_Q_CLARITY = {
    "key": "clarity",
    "ack": None,
    "message": "How sure are you about what you're targeting? This helps me figure out how much to narrow things down vs. keep options open.",
    "mcq": {
        "question": "How clear are you on what you want?",
        "options": [
            {"label": "A", "text": "I know exactly, give me precise controls"},
            {"label": "B", "text": "I have a general direction, help me narrow it down"},
            {"label": "C", "text": "Still figuring it out, keep it simple"},
        ],
        "allow_multiple": False,
    },
    "text_input": False,
}

_Q2_JOB_TYPE = {
    "key": "job_type",
    "ack": None,
    "message": "What kind of opportunity are you actually after right now?",
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

# _Q4_COMPANY_STAGE and _Q5_CAREER_GOAL are now built dynamically by
# _build_company_stage_question() and _build_career_goal_question()
# to inject resume and answer-aware suggestions into the messages.

_Q8_WORK_MODE = {
    "key": "work_mode",
    "ack": None,
    "message": "Almost there. Where do you actually want to be working from?",
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

# Niche options — ordered here, but dynamically reordered by _build_niche_question
_NICHE_OPTIONS_BASE = [
    "Fintech / Payments",
    "SaaS / B2B",
    "Consumer apps / D2C",
    "AI / ML",
    "Edtech",
    "Healthtech / Biotech",
    "Devtools / Developer-first",
    "Logistics / Supply chain",
    "Climate / Sustainability",
    "Gaming / Media",
    "Agritech",
    "No strong preference",
]

# Maps v2 vertical_exposure values to their display niche option
_VERTICAL_TO_NICHE = {
    "fintech": "Fintech / Payments",
    "payments": "Fintech / Payments",
    "insurtech": "Fintech / Payments",
    "regtech": "Fintech / Payments",
    "saas": "SaaS / B2B",
    "b2b-saas": "SaaS / B2B",
    "b2b": "SaaS / B2B",
    "enterprise-software": "SaaS / B2B",
    "consumer": "Consumer apps / D2C",
    "d2c": "Consumer apps / D2C",
    "ecommerce": "Consumer apps / D2C",
    "consumer-tech": "Consumer apps / D2C",
    "ai": "AI / ML",
    "ml": "AI / ML",
    "voice-ai": "AI / ML",
    "generative-ai": "AI / ML",
    "ai-native": "AI / ML",
    "nlp": "AI / ML",
    "llm": "AI / ML",
    "edtech": "Edtech",
    "education": "Edtech",
    "career-tech": "Edtech",
    "hr-tech": "Edtech",
    "healthtech": "Healthtech / Biotech",
    "biotech": "Healthtech / Biotech",
    "healthcare": "Healthtech / Biotech",
    "medtech": "Healthtech / Biotech",
    "devtools": "Devtools / Developer-first",
    "developer-tools": "Devtools / Developer-first",
    "infrastructure": "Devtools / Developer-first",
    "cybersecurity": "Devtools / Developer-first",
    "logistics": "Logistics / Supply chain",
    "supply-chain": "Logistics / Supply chain",
    "proptech": "SaaS / B2B",
    "legaltech": "SaaS / B2B",
    "martech": "SaaS / B2B",
    "adtech": "SaaS / B2B",
    "climate": "Climate / Sustainability",
    "cleantech": "Climate / Sustainability",
    "sustainability": "Climate / Sustainability",
    "gaming": "Gaming / Media",
    "media": "Gaming / Media",
    "agritech": "Agritech",
    "agriculture": "Agritech",
}


def _build_niche_question(resume_profile: dict) -> dict:
    """Build niche question, reordering options to front-rank verticals from v2 vertical_exposure."""
    vertical_exposure: list = resume_profile.get("vertical_exposure") or []

    # Map each vertical_exposure entry to a niche option label
    matched_niches: list[str] = []
    for v in (vertical_exposure if isinstance(vertical_exposure, list) else []):
        niche = _VERTICAL_TO_NICHE.get(v.lower().replace(" ", "-"))
        if niche and niche not in matched_niches and niche != "No strong preference":
            matched_niches.append(niche)

    # Reorder: matched first, then remaining, always with "No strong preference" last
    remaining = [n for n in _NICHE_OPTIONS_BASE if n not in matched_niches and n != "No strong preference"]
    ordered = matched_niches + remaining + ["No strong preference"]
    options = [{"label": chr(65 + i), "text": n} for i, n in enumerate(ordered)]

    if matched_niches:
        vertical_str = " and ".join(matched_niches[:2])
        message = (
            f"Based on your background in {vertical_str}, these spaces probably feel most natural. "
            f"Pick up to 3 and we'll focus the search there."
        )
    else:
        message = "Which of these spaces genuinely excites you? Pick up to 3 and we'll focus the search there."

    return {
        "key": "niche_keywords",
        "ack": None,
        "message": message,
        "mcq": {
            "question": "Which niches do you want us to prioritize?",
            "options": options,
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
        "ack": None,
        "message": "Which tools do you actually want to be working with day-to-day? Pick up to 3.",
        "mcq": {
            "question": "Top 3 tools/tech you want to use?",
            "options": options,
            "allow_multiple": True,
        },
        "text_input": False,
    }

# ---------------------------------------------------------------------------
# Contextual ack generator -- warm, answer-aware acknowledgements
# ---------------------------------------------------------------------------

def build_contextual_ack(prev_q_key: str, prev_answer: str | None, resume_profile: dict) -> str:
    """Generate a warm, specific acknowledgement of the previous answer.

    Echoes back what the user said plus a brief observation.
    Purely rule-based, zero latency.
    """
    answer = (prev_answer or "").strip()
    al = answer.lower()

    if prev_q_key == "career_stage":
        if "not graduating" in al:
            return "Nice, early in the journey. Plenty of time to be strategic about where you land."
        if "graduating" in al or "6 months" in al:
            return "Exciting timing. The market moves fast and now is exactly when to get ahead of it."
        if "recent graduate" in al or "0-2" in al:
            return "Fresh into it, and honestly that's the best time to go on the offensive with outreach."
        if "experienced" in al or "3+" in al:
            return "Solid base to work from."
        if "switching" in al or "exploring" in al:
            return "A career pivot can be one of the best moves you make. Let's find companies that'll value the cross-functional perspective."
        return "Got it."

    if prev_q_key == "clarity":
        if "exactly" in al or al.startswith("a"):
            return "Love it. The more specific you are, the sharper the targeting."
        if "general" in al or "narrow" in al or al.startswith("b"):
            return "That's honestly the most common starting point. We'll help you zero in."
        if "figuring" in al or "simple" in al or al.startswith("c"):
            return "Totally fine, we'll keep it open and let the options speak for themselves."
        return "Got it."

    if prev_q_key == "job_type":
        if "full-time" in al:
            return "Full-time it is."
        if "internship" in al:
            return "Internship mode, let's find you a good one."
        if "part-time" in al or "freelance" in al:
            return "Part-time or freelance, keeping it flexible. Got it."
        if "open to all" in al:
            return "Open to everything, gives us the most to work with."
        return "Got it."

    if prev_q_key == "location":
        if "remote" in al and "relocate" not in al:
            return "Remote, no office and no schedule constraints. Makes sense."
        if "relocate" in al or "international" in al:
            return "Open to relocating, that expands the universe significantly."
        _city_map = {
            "bengaluru": "Bengaluru, strong ecosystem and a lot of the best companies are right there.",
            "bangalore": "Bengaluru, strong ecosystem and a lot of the best companies are right there.",
            "mumbai": "Mumbai, financial hub with a lot to work with.",
            "delhi": "Delhi/NCR, great mix of MNCs and home-grown startups.",
            "hyderabad": "Hyderabad, solid tech scene growing fast.",
            "pune": "Pune, increasingly strong startup and tech ecosystem.",
            "chennai": "Chennai, great SaaS and tech presence.",
        }
        for city_key, resp in _city_map.items():
            if city_key in al:
                return resp
        return "Location noted."

    if prev_q_key == "company_stage":
        company_type_avoid: list = resume_profile.get("company_type_avoid") or []
        avoid_enterprise = any(t in company_type_avoid for t in ("large-mnc", "enterprise", "corporate"))
        avoid_early = any(t in company_type_avoid for t in ("pre-seed", "early-stage", "seed-stage"))
        if "early" in al or "seed" in al:
            if avoid_early:
                return "Early-stage, worth noting your background tends to thrive with more structure. But if that energy is what you want, go for it."
            return "Early-stage, where things actually move. Good call."
        if "growth" in al or "50-500" in al:
            return "Growth-stage, sweet spot of structure and speed."
        if "large" in al or "mnc" in al or "enterprise" in al:
            if avoid_enterprise:
                return "Enterprise noted. Just flagging that your profile tends to fit faster-moving environments better. Your call though."
            return "Enterprise, stability and scale. Noted."
        if "no strong" in al:
            return "Open book, we'll cast a wide net and let fit show itself."
        return "Good to know."

    if prev_q_key == "career_goal":
        return "Clear picture, that's exactly what we need to write a sharp pitch."

    if prev_q_key == "dream_companies":
        if al in ("skip", "none", "n/a", "na", ""):
            return "No worries, we'll figure out the right companies from your profile."
        return "Good companies to calibrate against."

    if prev_q_key == "target_role":
        return "Noted, that narrows the search nicely."

    if prev_q_key == "work_mode":
        if "remote" in al:
            return "Remote, we'll filter for companies that genuinely support it, not just list it."
        if "hybrid" in al:
            return "Hybrid, most common setup at good companies right now."
        if "in-office" in al or ("office" in al and "remote" not in al):
            return "In-office, we'll focus on companies in your city."
        if "open to all" in al:
            return "Open to all, we'll match on fit first and setup second."
        return "Got it."

    if prev_q_key == "niche_keywords":
        return "Those spaces have a lot of action right now. Good picks."

    if prev_q_key == "tech_stack":
        return "Stack noted, we'll weight companies that use those tools."

    if prev_q_key == "flex_best_project":
        return "That's a strong signal, exactly the kind of thing that lands well in outreach."

    if prev_q_key == "flex_outcome":
        return "Numbers make the story concrete. Good."

    return "Got it."


# ---------------------------------------------------------------------------
# Resume + answer-aware question builders (company stage, career goal)
# ---------------------------------------------------------------------------

_STARTUP_HUB_CITIES = {
    "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune",
    "chennai", "gurgaon", "noida", "kolkata", "ahmedabad",
    "san francisco", "new york", "london", "singapore", "berlin",
}

_AI_SUBDOMAINS = {
    "ai_engineering", "ml_engineering", "data_science", "ai_product",
    "llm", "generative_ai", "nlp", "computer_vision", "voice_ai",
}


def _infer_stage_suggestion(resume_profile: dict, answers: dict) -> str | None:
    """Return a 1-2 sentence stage suggestion, using v2 fields when available.

    Priority: v2 company_stage_fit > v2 archetype_label > domain/subdomain signals > answer signals.
    """
    # --- v2 fields (populated when extract_enhanced_resume_profile ran) ---
    company_stage_fit: list = resume_profile.get("company_stage_fit") or []
    archetype_label: str = (resume_profile.get("archetype_label") or "").strip()
    company_type_best_fit: list = resume_profile.get("company_type_best_fit") or []

    if company_stage_fit:
        early = any(s in company_stage_fit for s in ("pre-seed", "seed", "series-a"))
        growth = any(s in company_stage_fit for s in ("series-b", "growth"))
        enterprise = "enterprise" in company_stage_fit

        if archetype_label and early:
            ai_type = any(t in (company_type_best_fit or []) for t in ("ai-native-startup", "automation-agency", "vertical-saas"))
            if ai_type:
                return f"Your profile reads as a {archetype_label}. AI-native early-stage companies are where that profile is most in demand right now."
            return f"Your profile reads as a {archetype_label}. Early-stage companies are where that kind of end-to-end ownership tends to be valued most."
        if early and not enterprise:
            return "Based on your background, early-stage companies are where you'd likely have the most impact and ownership."
        if growth and not early:
            return "Based on your background, growth-stage companies look like the strongest fit: real scale but still moving fast."
        if enterprise and not early and not growth:
            return "Your background maps well to established companies with structured environments."

    # --- v1 / standard fields fallback ---
    domain = (resume_profile.get("domain") or "").lower()
    subdomain = (resume_profile.get("subdomain") or "").lower()
    seniority = (resume_profile.get("seniority") or "").lower()
    top_skills = [s.lower() for s in (resume_profile.get("top_skills") or [])]
    likely_roles = " ".join(resume_profile.get("likely_roles") or []).lower()

    career_stage_ans = answers.get("career_stage", "").lower()
    location_ans = answers.get("location", "").lower()
    job_type_ans = answers.get("job_type", "").lower()

    in_startup_hub = any(h in location_ans for h in _STARTUP_HUB_CITIES)
    is_ai_profile = (
        subdomain in _AI_SUBDOMAINS
        or any(kw in top_skills for kw in ("ai", "llm", "openai", "machine learning", "pytorch"))
        or any(kw in likely_roles for kw in ("ai engineer", "ml engineer", "data scientist", "ai agent"))
        or domain in ("data", "data_analytics")
    )
    is_student = "student" in career_stage_ans or seniority == "student"
    is_internship = "internship" in job_type_ans

    if is_ai_profile and is_internship and in_startup_hub:
        city = next(
            (c for c in ["Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune"]
             if c.lower() in location_ans or c.lower()[:4] in location_ans),
            None,
        )
        city_str = f"{city} has" if city else "Your city has"
        return (
            f"{city_str} a strong AI startup scene right now. "
            f"Early-stage companies are where you'll get real AI work from day one rather than scoped tasks."
        )
    if is_ai_profile and is_internship:
        return "For AI internships, early-stage startups tend to give you the most hands-on work from day one."
    if is_ai_profile and not is_internship and seniority in ("junior", "mid"):
        return "Your AI background maps well to early-stage or growth-stage companies right now. Both are actively hiring in this space."
    if is_ai_profile and not is_internship and seniority == "senior":
        return "With your background, growth-stage AI companies are often the sweet spot: real scale and still moving fast."
    if is_student and is_internship and in_startup_hub:
        return "Early-stage startups tend to be the sharpest learning environment for internships, especially in a hub like yours."
    if "experienced" in career_stage_ans or seniority in ("mid", "senior"):
        return "With your experience level, growth-stage companies are often the sweet spot: real scale but still moving fast enough to matter."

    return None


def _build_company_stage_question(resume_profile: dict, answers: dict) -> dict:
    """Build the company stage question with a profile and answer-aware suggestion."""
    suggestion = _infer_stage_suggestion(resume_profile, answers)
    if suggestion:
        message = f"What kind of company environment are you after? {suggestion} But you know yourself best, pick what actually resonates."
    else:
        message = "What kind of company environment are you targeting?"

    return {
        "key": "company_stage",
        "ack": None,
        "message": message,
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


def _build_career_goal_question(resume_profile: dict, answers: dict) -> dict:
    """Build the career goal question with a day-to-day suggestion from v2 archetype or domain signals."""
    archetype_label: str = (resume_profile.get("archetype_label") or "").strip()
    domain = (resume_profile.get("domain") or "").lower()
    subdomain = (resume_profile.get("subdomain") or "").lower()
    top_skills = [s.lower() for s in (resume_profile.get("top_skills") or [])]
    company_stage_ans = answers.get("company_stage", "").lower()
    is_early = "early" in company_stage_ans or "seed" in company_stage_ans

    suggestion = None
    placeholder = "e.g. Running GTM for an early-stage AI startup, Closing enterprise deals in SaaS"

    # v2: use archetype_label for a sharp, personalised suggestion
    _archetype_day_to_day = {
        "founding product engineer": "shipping AI products from zero to one, talking to users, and owning the full stack from idea to deployment",
        "ai-native founder's office hire": "owning cross-functional execution across product, growth, and ops at a fast-moving AI startup",
        "zero-to-one growth systems builder": "building and running growth systems end-to-end, from acquisition to activation",
        "applied ai solutions architect": "designing and delivering AI automations for clients, owning the whole solution from scoping to deployment",
        "gtm automation engineer": "building automated GTM systems that turn outbound into a repeatable, scalable motion",
        "full-stack ai builder": "designing, building, and deploying AI-powered products end-to-end",
        "product-led growth operator": "owning the product growth loop, running experiments, and turning activation into retention",
        "ai product strategist": "sitting at the intersection of AI engineering and product, deciding what to build and then building it",
        "founder's office": "owning execution across product, growth, and ops for a fast-moving founding team",
        "ai automation consultant": "diagnosing business problems and building AI-powered automations that save real time and money",
    }

    archetype_lower = archetype_label.lower()
    for key, day_to_day in _archetype_day_to_day.items():
        if key in archetype_lower or archetype_lower in key:
            suggestion = day_to_day
            placeholder = f"e.g. {day_to_day[:80]}..."
            break

    # fallback to domain signals if no archetype match
    if not suggestion:
        is_ai = subdomain in _AI_SUBDOMAINS or any(kw in top_skills for kw in ("ai", "llm", "openai", "langchain", "pytorch"))
        if is_ai:
            suggestion = "building and shipping AI features end-to-end at a fast-moving startup" if is_early else "designing and shipping AI systems, owning a product surface end-to-end"
            placeholder = "e.g. Building and deploying AI agents end-to-end at a seed-stage startup"
        elif domain == "software_engineering":
            suggestion = "writing code, shipping features, and owning a product area end-to-end"
            placeholder = "e.g. Building backend systems for a fast-moving B2B SaaS startup"
        elif domain in ("data_analytics", "data"):
            suggestion = "running analysis, building dashboards, and helping the team make better decisions with data"
            placeholder = "e.g. Building the analytics stack and owning data insights for a growth-stage startup"
        elif domain == "marketing":
            suggestion = "running growth experiments, owning user acquisition channels, and iterating fast on what works"
            placeholder = "e.g. Running performance marketing and owning the growth funnel at a Series A startup"
        elif domain in ("product", "product_management"):
            suggestion = "writing specs, talking to users, and working closely with eng to ship product"
            placeholder = "e.g. Owning a product vertical from roadmap to launch at a fast-moving startup"
        elif domain in ("sales", "sales_bd"):
            suggestion = "closing deals, managing a pipeline, and owning revenue targets"
            placeholder = "e.g. Running outbound, closing pilots, and building the early revenue engine"
        elif domain in ("consulting", "consulting_strategy"):
            suggestion = "framing problems, running structured analysis, and turning insights into decisions"
            placeholder = "e.g. Supporting strategy work for high-growth startups or running client engagements"
        elif domain == "finance":
            suggestion = "building financial models and supporting investment or strategy decisions"
            placeholder = "e.g. Building the financial model for a Series B raise, owning FP&A at a growth-stage company"

    if suggestion:
        message = (
            f"Paint me a picture of your ideal day-to-day. "
            f"Based on your background I'd guess something like {suggestion}. "
            f"Does that track, or do you have a different picture?"
        )
    else:
        message = "Paint me a picture of your ideal day-to-day. One sentence is enough."

    return {
        "key": "career_goal",
        "ack": None,
        "message": message,
        "mcq": None,
        "text_input": True,
        "input_placeholder": placeholder,
    }


# ---------------------------------------------------------------------------
# Flex (email-personalization) prompts — role-adaptive
# ---------------------------------------------------------------------------

_FLEX_PROJECT_COPY: dict[str, tuple[str, str]] = {
    # cluster → (prompt, placeholder)
    "engineering": (
        "What's a thing you built that someone actually used? Tell me what it did and roughly how big the impact was.",
        "e.g. Built a Telegram bot that auto-summarizes my college's placement-cell announcements, used by 800+ students",
    ),
    "data": (
        "What's a data project or analysis you ran that drove a decision? What did you investigate and what changed?",
        "e.g. Built a churn-prediction model for a fintech client that flagged 12% of accounts for proactive outreach",
    ),
    "product": (
        "What's a product or feature you shipped (or co-shipped)? What was it, who was it for, and what changed?",
        "e.g. Shipped a referral feature for a commerce app that drove 22% of new signups in month one",
    ),
    "design": (
        "What's a design project you shipped that someone actually used? Who used it and what did it improve?",
        "e.g. Redesigned the onboarding flow for an edtech app, drop-off after step 1 fell from 50% to 18%",
    ),
    "marketing": (
        "What's a campaign or growth experiment you ran that worked? What did you try and what shifted as a result?",
        "e.g. Ran an Instagram launch campaign that took a D2C brand from 2k to 8k followers in 6 weeks",
    ),
    "sales": (
        "What's a deal or partnership you closed (or got close to)? Who was the buyer and how did you move it?",
        "e.g. Closed a 20-seat pilot with a Bengaluru SaaS company after 4 months of cold outbound",
    ),
    "finance": (
        "What's a finance / analysis project you owned? What was the question and what call did you help make?",
        "e.g. Built the financial model for a Series B raise that closed at $30M",
    ),
    "consulting": (
        "What's a project or engagement you delivered that mattered? Who was the client and what shifted for them?",
        "e.g. Led a 6-week ops audit for a D2C brand, identified 3 changes that cut fulfilment cost 18%",
    ),
    "other": (
        "What's a project or piece of work you're most proud of? What was it and what came of it?",
        "e.g. Organized a city-wide hackathon with 200+ participants and 6 sponsor companies",
    ),
}

_FLEX_OUTCOME_COPY: dict[str, tuple[str, str]] = {
    "engineering": (
        "What's the most concrete number you can share about it? Even rough is fine.",
        "e.g. ~800 weekly users, runtime cut from 12s to 2s, shipped to prod in one sprint",
    ),
    "data": (
        "What's the most concrete number you can share? Numbers > adjectives.",
        "e.g. $2M ARR identified at risk, 89% precision, dashboard now used by 3 ops teams",
    ),
    "product": (
        "What's the most concrete number you can share? Anything quantitative: adoption, retention, revenue moved.",
        "e.g. 22% of new signups via referral, retention +14%, shipped under a 6-week PRD",
    ),
    "design": (
        "What's the most concrete number you can share?",
        "e.g. Activation up 32%, used daily by 12k people, sprint shipped on time",
    ),
    "marketing": (
        "What's the most concrete number you can share? Numbers > adjectives.",
        "e.g. CAC dropped 60%, 4x engagement, $15k attributed revenue in month one",
    ),
    "sales": (
        "What's the deal size or pipeline number?",
        "e.g. ~$8k ARR, became the company's first enterprise reference customer",
    ),
    "finance": (
        "What's the most concrete number? Deal size, valuation, cost saved, anything.",
        "e.g. Raise closed at $30M with 18x revenue multiple, model used in IC pitch",
    ),
    "consulting": (
        "What's the most concrete result? Cost saved, time cut, revenue lift.",
        "e.g. Fulfilment cost down 18%, recommendations adopted within 30 days",
    ),
    "other": (
        "What's the most concrete result you can share? Even one number is fine.",
        "e.g. 200 participants, 6 sponsors, raised ₹2L; featured on YourStory",
    ),
}


def _build_flex_project_question(cluster_key: str, resume_profile: dict | None = None) -> dict:
    prompt, placeholder = _FLEX_PROJECT_COPY.get(cluster_key, _FLEX_PROJECT_COPY["other"])
    strongest_hook: str = ((resume_profile or {}).get("strongest_hook") or "").strip()
    if strongest_hook:
        hook = strongest_hook.rstrip(".")
        # Capitalise first char, lowercase the rest for mid-sentence fit
        hook_lower = hook[0].lower() + hook[1:] if hook else hook
        message = f"Based on your resume, you've {hook_lower}. Tell me about the specific project behind that."
    else:
        message = prompt
    return {
        "key": "flex_best_project",
        "ack": None,
        "message": message,
        "mcq": None,
        "text_input": True,
        "input_placeholder": placeholder,
    }


def _build_flex_outcome_question(cluster_key: str) -> dict:
    prompt, placeholder = _FLEX_OUTCOME_COPY.get(cluster_key, _FLEX_OUTCOME_COPY["other"])
    return {
        "key": "flex_outcome",
        "ack": None,
        "message": prompt,
        "mcq": None,
        "text_input": True,
        "input_placeholder": placeholder,
    }

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _clarity_level(answers: dict) -> str:
    """Map the explicit clarity answer (Q2) to one of: high / medium / low.

    Falls back to "medium" if the question hasn't been answered yet
    (so the niche question still shows up in the planning sequence).
    """
    raw = (answers.get("clarity") or "").lower()
    if "exactly" in raw or "precise" in raw or raw.startswith("a"):
        return "high"
    if "general" in raw or "narrow" in raw or raw.startswith("b"):
        return "medium"
    if "figuring" in raw or "simple" in raw or raw.startswith("c"):
        return "low"
    return "medium"


# Backwards-compat shim: routes_discovery still imports this helper to
# annotate the CandidateProfile. Returns 0 so existing call sites continue
# to work without spreading clarity-as-int assumptions further.
def _clarity_score(answers: dict, resume_profile: dict) -> int:  # noqa: ARG001
    level = _clarity_level(answers)
    return {"high": 3, "medium": 1, "low": -1}.get(level, 1)


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


# Map detected cluster → flex copy bucket
_CLUSTER_TO_FLEX_COPY = {
    "software_engineering": "engineering",
    "data_analytics": "data",
    "data": "data",
}


def _flex_copy_key(answers: dict, resume_profile: dict) -> str:
    """Pick the flex prompt bucket from cluster + target_role + resume.

    v2 override: if distribution_signal=True on a technical profile, use "product"
    copy — their stories are about impact and distribution, not just code shipped.
    """
    cluster = _detect_role_cluster(answers, resume_profile)
    distribution_signal: bool = bool(resume_profile.get("distribution_signal"))
    execution_style: str = (resume_profile.get("execution_style") or "").lower()

    # v2: builder/engineer with distribution signal → product copy fits better
    if cluster in ("software_engineering", "ml_engineering") and distribution_signal:
        return "product"
    # v2: execution_style "builder" with growth/gtm archetype hint
    if "builder" in execution_style and "gtm" in (resume_profile.get("archetype_label") or "").lower():
        return "product"

    if cluster in _CLUSTER_TO_FLEX_COPY:
        return _CLUSTER_TO_FLEX_COPY[cluster]
    target = (answers.get("target_role") or "").lower()
    if any(kw in target for kw in ("product manager", "product owner", "apm")):
        return "product"
    if any(kw in target for kw in ("designer", "design lead", "ux", "ui")):
        return "design"
    if any(kw in target for kw in ("growth", "marketing", "brand", "content", "seo", "gtm")):
        return "marketing"
    if any(kw in target for kw in ("sales", "account executive", "business development", "bdr", "sdr")):
        return "sales"
    if any(kw in target for kw in ("finance", "investment", "valuation", "fp&a")):
        return "finance"
    if any(kw in target for kw in ("consult", "strategy", "advisory")):
        return "consulting"
    # Resume domain fallback
    domain = (resume_profile.get("domain") or "").lower()
    if domain in ("marketing",):
        return "marketing"
    if domain in ("sales", "sales_bd"):
        return "sales"
    if domain in ("finance",):
        return "finance"
    if domain in ("consulting", "consulting_strategy"):
        return "consulting"
    if domain in ("product", "product_management"):
        return "product"
    if domain in ("design",):
        return "design"
    return "other"


def build_question_sequence(state: dict) -> list[dict]:
    """
    Return the full ordered list of questions for this candidate.
    Branching is driven by the explicit `clarity` answer (Q2), not an
    implicit score. Flex (project + outcome) questions are now part of
    the main quiz — no post-payment intercept.
    """
    answers = state.get("answers", {})
    resume_profile = state.get("resume_profile") or {}
    resume_text = state.get("resume_text") or ""
    parsed_json = state.get("parsed_json") or {}

    stage = answers.get("career_stage", "").lower()
    skip_job_type = any(kw in stage for kw in ("experienced", "3+", "switching"))

    sequence = [_Q1_CAREER_STAGE, _Q_CLARITY]
    if not skip_job_type:
        sequence.append(_Q2_JOB_TYPE)
    sequence.append(_build_location_question(resume_profile, resume_text, parsed_json))
    sequence.append(_build_company_stage_question(resume_profile, answers))
    sequence.append(_build_career_goal_question(resume_profile, answers))
    sequence.append(_build_dream_companies_question(answers, resume_profile))
    sequence.append(_build_target_role_question(resume_profile, parsed_json))
    sequence.append(_Q8_WORK_MODE)

    clarity = _clarity_level(answers)

    # Niche keywords — asked for medium + high (skipped only on "still figuring out")
    if clarity in ("medium", "high"):
        sequence.append(_build_niche_question(resume_profile))

    # Tech stack — asked for high clarity AND technical cluster
    if clarity == "high":
        cluster = _detect_role_cluster(answers, resume_profile)
        ts_q = _build_tech_stack_question(cluster)
        if ts_q is not None:
            sequence.append(ts_q)

    # Flex (email-fuel) questions — always asked, role-adaptive copy
    flex_key = _flex_copy_key(answers, resume_profile)
    sequence.append(_build_flex_project_question(flex_key, resume_profile))
    sequence.append(_build_flex_outcome_question(flex_key))

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


def build_message(
    q_def: dict,
    prev_q_key: str | None,
    is_first: bool,
    prev_answer: str | None = None,
    resume_profile: dict | None = None,
) -> str:
    """Build the SSE message text: contextual ack for the previous answer + this question.

    The `|||` separator is the contract with the frontend — it splits into two
    separate chat bubbles: the ack and the new question.
    """
    if is_first or not prev_q_key:
        return q_def["message"]
    ack = build_contextual_ack(prev_q_key, prev_answer, resume_profile or {})
    return f"{ack}|||{q_def['message']}"


# ---------------------------------------------------------------------------
# Adaptive question builders
# ---------------------------------------------------------------------------


# Maps v2 company_type_best_fit values to aspirational example companies
_COMPANY_TYPE_EXAMPLES = {
    "ai-native-startup": "ElevenLabs, Glean, Cohere",
    "vertical-saas": "Rippling, Ironclad, Leapwork",
    "b2b-saas": "Notion, Linear, Loom",
    "fintech": "Razorpay, Stripe, Brex",
    "devtools": "Vercel, Supabase, Railway",
    "automation-agency": "Bardeen, Copy.ai, Jasper",
    "consulting-firm": "McKinsey, BCG, Bain",
    "large-mnc": "Google, Microsoft, Goldman Sachs",
    "growth-stage-startup": "Swiggy, Meesho, Gupshup",
    "deep-tech": "DeepMind, OpenAI, Mistral",
    "consumer-tech": "Zepto, Blinkit, CRED",
    "edtech": "Teachable, Maven, Outlier",
}


def _build_dream_companies_question(answers: dict, resume_profile: dict | None = None) -> dict:
    """
    Build Q6 dream companies question — text input with adaptive examples
    based on company_type_best_fit (v2) or company_stage answer.
    """
    stage = (answers.get("company_stage") or "").lower()
    profile = resume_profile or {}

    # v2: use company_type_best_fit for sharp, profile-matched examples
    company_type_best_fit: list = profile.get("company_type_best_fit") or []
    examples = None
    for ctype in company_type_best_fit:
        examples = _COMPANY_TYPE_EXAMPLES.get(ctype)
        if examples:
            break

    # fallback to stage-based defaults
    if not examples:
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
        "ack": None,
        "message": (
            f"Any companies you'd genuinely love to work at? Even stretch goals are useful, "
            f"they tell me a lot about the type of company you're drawn to. "
            f"Separate with commas. e.g. '{examples}' (or type 'skip' if none come to mind)"
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
        "ack": None,
        "message": "Which cities are you actually open to working in?",
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

    Uses WORD-BOUNDARY regex matching. Naive substring matching previously
    caused 'evaluation' to match 'valuation' (finance), 'pipeline' to match
    sales pipeline, etc. — misclassifying ML engineers as finance/sales.
    """
    import re

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
            ("account executive", 3), ("sales strategy", 2),
            ("sales pipeline", 3), ("deal closing", 3), ("client acquisition", 2),
            ("sales", 1), ("quota", 2),
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
            # NOTE: dropped bare "valuation" — common in ML "model evaluation" contexts
            ("dcf valuation", 4), ("company valuation", 3), ("financial valuation", 3),
            ("financial analysis", 2), ("finance", 1), ("investments", 1),
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
            ("microservices", 3), ("system design", 3), ("kubernetes", 2),
            ("coding", 2), ("programming", 2), ("git", 1), ("developer", 1),
        ],
        # NEW: ML/AI engineering domain — needed because the old data_analytics
        # bucket pulled BI/Reporting roles (Data Analyst, BI Analyst) for ML engineers.
        "ml_engineering": [
            ("machine learning engineer", 5), ("ml engineer", 5), ("ai engineer", 5),
            ("nlp engineer", 5), ("computer vision engineer", 5), ("research scientist", 4),
            ("deep learning", 4), ("large language model", 4), ("llm", 3), ("nlp", 3),
            ("asr", 4), ("speech recognition", 4), ("conversational ai", 4),
            ("transformer", 3), ("pytorch", 3), ("tensorflow", 3), ("hugging face", 3),
            ("generative ai", 4), ("gen ai", 4), ("ai agent", 4),
            ("model training", 3), ("fine-tuning", 3), ("rag", 3), ("retrieval augmented", 4),
            ("ml model", 3), ("ai model", 3), ("inference latency", 3),
            ("machine learning", 2), ("artificial intelligence", 2),
        ],
        "data_analytics": [
            ("data analyst", 5), ("bi analyst", 5), ("business intelligence", 4),
            ("data science", 3), ("sql queries", 3), ("tableau", 3), ("power bi", 3),
            ("statistical analysis", 3), ("analytics", 2),
            ("data analysis", 2), ("data-driven", 2), ("dashboard", 2),
            ("etl pipeline", 3), ("data warehouse", 3),
        ],
        "design": [
            ("ux designer", 4), ("product designer", 4), ("ui/ux", 4),
            ("user experience design", 4), ("figma", 3), ("wireframe", 3),
            ("interaction design", 3), ("ux research", 2), ("prototyping", 2),
            ("user interface", 2),
        ],
        "hr_people": [
            ("talent acquisition", 4), ("human resources", 3), ("people operations", 3),
            ("technical recruiter", 3), ("hr manager", 3), ("employee engagement", 2),
            ("performance management", 2), ("recruiting", 2),
        ],
    }

    lower = text[:3000].lower()
    scores: dict[str, int] = {}
    for domain, keyword_weights in DOMAIN_KEYWORDS.items():
        total = 0
        for kw, weight in keyword_weights:
            # Word-boundary regex: 'valuation' won't match 'evaluation'.
            # re.escape handles &, /, etc. in keywords like 'm&a' or 'ui/ux'.
            pattern = r"\b" + re.escape(kw) + r"\b"
            count = len(re.findall(pattern, lower))
            if count > 0:
                total += count * weight
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
        "ml_engineering": ("Data & Analytics", ["data science", "ml"]),
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

    # v2: prepend archetype_label as first option if it isn't already covered
    archetype_label: str = (profile.get("archetype_label") or "").strip()
    if archetype_label:
        archetype_lower = archetype_label.lower()
        already_covered = any(archetype_lower in r.lower() or r.lower() in archetype_lower for r in ontology_roles)
        if not already_covered:
            ontology_roles.insert(0, archetype_label)

    # Deduplicate, cap at 6, add "Something else"
    seen, final_roles = set(), []
    for r in ontology_roles:
        if r not in seen:
            seen.add(r)
            final_roles.append(r)
        if len(final_roles) == 6:
            break

    options = [{"label": chr(65 + i), "text": r} for i, r in enumerate(final_roles)]
    options.append({"label": chr(65 + len(final_roles)), "text": "Something else"})

    msg = (
        f"Which of these roles feels closest to what you're actually going after? "
        f"I pulled these from your background"
        + (f", and '{archetype_label}' is what your profile most reads as" if archetype_label else "")
        + ". If none fit just pick 'Something else'."
    )

    return {
        "key": "target_role",
        "ack": None,
        "message": msg,
        "mcq": {
            "question": "Which role fits best?",
            "options": options,
            "allow_multiple": True,
        },
        "text_input": False,
    }
