"""Email Generator Service — Structured pipeline for human-sounding outreach.

Generates emails through a multi-stage pipeline:
  candidate_profile_extraction -> lead_profile_extraction -> template_selection
  -> structured_email_generation -> tone_cleaner -> final_email

Every email follows a strict 5-part structure:
  1. Greeting
  2. Micro hook (company/role reference)
  3. Candidate signal (one concrete resume signal)
  4. Relevance bridge (why this person)
  5. Soft ask + human closing

Hard rules: 70-120 words, 3-5 sentences, no em dashes, no AI phrasing.
"""

import random
import re
from typing import Tuple, Dict

from database.models import Lead, Candidate
from services.shared.ai.azure_openai_client import generate_json
from core.logger import get_logger

logger = get_logger(__name__)

# ── Style definitions ──────────────────────────────────────────────────────────

STYLE_DESCRIPTIONS = {
    "warm_intro": {
        "tone": "Warm, genuine, humble",
        "hook_style": "mention something you noticed about their team or role",
        "soft_ask": "ask if they'd be open to pointing you in the right direction",
    },
    "value_prop": {
        "tone": "Professional, specific, grounded",
        "hook_style": "reference a specific area their team works on that relates to your skills",
        "soft_ask": "ask if there's someone on their team you should talk to",
    },
    "company_curiosity": {
        "tone": "Genuinely curious, low-key",
        "hook_style": "mention something the company is building or working on that caught your eye",
        "soft_ask": "ask if they have a few minutes to chat about what their team is working on",
    },
    "peer_to_peer": {
        "tone": "Casual, collegial",
        "hook_style": "connect on a shared technical interest or tool",
        "soft_ask": "suggest a quick chat about shared interests",
    },
    "direct_ask": {
        "tone": "Concise, respectful, no fluff",
        "hook_style": "state directly why you're reaching out to them specifically",
        "soft_ask": "ask clearly if they know of any open roles or who to contact",
    },
}

# ── Variation pools ────────────────────────────────────────────────────────────

GREETINGS = ["Hi {name},", "Hey {name},", "Hi {name} -"]

CLOSINGS = [
    "Appreciate your time either way.",
    "Thanks for reading.",
    "Would appreciate any direction.",
    "No worries if not, thanks for reading.",
    "Either way, appreciate you taking a look.",
    "Thanks in advance.",
]

SIGNOFFS = [
    "Best,\n{name}",
    "Cheers,\n{name}",
    "Thanks,\n{name}",
    "{name}",
]

# ── Forbidden patterns ─────────────────────────────────────────────────────────

FORBIDDEN_PHRASES = [
    "I hope this email finds you well",
    "I am passionate about",
    "excited to apply",
    "I believe my skills align",
    "I would be honored",
    "I admire your organization",
    "I was really impressed",
    "impressive work",
    "I came across your impressive",
    "commitment to innovation",
    "cutting-edge",
    "leverage my skills",
    "synergy",
    "align with your mission",
    "make a meaningful impact",
    "contribute to your team",
    "from day one",
    "bring to the table",
    "hit the ground running",
    "I'm eager to",
    "I'm excited to",
]


# ── Stage 1: Candidate Profile Extraction ──────────────────────────────────────

def extract_candidate_profile(candidate: Candidate) -> dict:
    """Extract a structured profile from the candidate's parsed resume JSON.

    Returns:
        dict with: candidate_name, education, key_skills, recent_project,
                   primary_field, job_interest, industries_of_interest,
                   short_candidate_signal
    """
    parsed = candidate.parsed_json or {}
    personal = parsed.get("personal_info", {})
    career = parsed.get("career_analysis", {})
    prefs = parsed.get("preferences", {})

    name = personal.get("name") or parsed.get("name") or ""

    # Skills
    skills = personal.get("skills_detected", []) or parsed.get("skills", [])
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",")]
    key_skills = skills[:3]

    # Education
    education = ""
    edu_list = personal.get("education", []) or parsed.get("education", [])
    if isinstance(edu_list, list) and edu_list:
        if isinstance(edu_list[0], dict):
            education = edu_list[0].get("degree", "") or edu_list[0].get("institution", "")
        elif isinstance(edu_list[0], str):
            education = edu_list[0]
    elif isinstance(edu_list, str):
        education = edu_list

    # Target roles
    recommended = career.get("recommended_roles", [])
    target_roles = [r.get("title", "") for r in recommended if r.get("title")]
    if not target_roles:
        target_roles = parsed.get("target_roles", []) or candidate.target_roles or []
    job_interest = target_roles[0] if target_roles else "software engineering"

    # Summary / primary field
    summary = parsed.get("profile_summary", "")
    primary_field = _infer_primary_field(skills, target_roles, summary)

    # Recent project
    recent_project = _extract_recent_project(parsed)

    # Industries
    industries = prefs.get("industries", []) or prefs.get("preferred_industries", [])
    if not industries and career.get("recommended_roles"):
        industries = list({r.get("industry", "") for r in career["recommended_roles"] if r.get("industry")})

    # Use flex_notes if available (post-payment answers — much more specific than resume parse)
    flex = candidate.flex_notes or {}
    if flex.get("best_project"):
        signal = flex["best_project"].strip()
        if flex.get("outcome"):
            signal += ". Outcome: " + flex["outcome"].strip()
    else:
        signal = _build_candidate_signal(name, primary_field, key_skills, recent_project, education)

    return {
        "candidate_name": name,
        "education": education,
        "key_skills": key_skills,
        "recent_project": recent_project,
        "primary_field": primary_field,
        "job_interest": job_interest,
        "industries_of_interest": industries[:3],
        "short_candidate_signal": signal,
        "has_flex_notes": bool(flex.get("best_project")),
    }


def _infer_primary_field(skills: list, roles: list, summary: str) -> str:
    """Infer the candidate's primary field from skills and roles."""
    combined = " ".join(skills + roles + [summary]).lower()
    fields = {
        "machine learning": ["machine learning", "ml", "deep learning", "neural", "tensorflow", "pytorch"],
        "data science": ["data science", "data analysis", "pandas", "statistics", "data engineer"],
        "web development": ["react", "angular", "vue", "frontend", "full stack", "fullstack", "next.js", "node"],
        "backend engineering": ["backend", "api", "django", "flask", "fastapi", "spring", "microservice"],
        "mobile development": ["ios", "android", "react native", "flutter", "swift", "kotlin"],
        "cloud engineering": ["aws", "azure", "gcp", "devops", "kubernetes", "docker", "infrastructure"],
        "blockchain": ["blockchain", "solidity", "web3", "smart contract", "ethereum"],
        "cybersecurity": ["security", "penetration", "cybersecurity", "soc", "threat"],
        "product management": ["product manager", "product management", "roadmap", "stakeholder"],
        "design": ["ui/ux", "figma", "design system", "user experience", "graphic design"],
    }
    for field, keywords in fields.items():
        if any(kw in combined for kw in keywords):
            return field
    return "software engineering"


def _extract_recent_project(parsed: dict) -> str:
    """Extract the most notable recent project from parsed resume data."""
    projects = parsed.get("projects", [])
    if isinstance(projects, list) and projects:
        if isinstance(projects[0], dict):
            return projects[0].get("name", "") or projects[0].get("title", "")
        elif isinstance(projects[0], str):
            return projects[0]

    # Try extracting from experience
    experience = parsed.get("experience", []) or parsed.get("work_experience", [])
    if isinstance(experience, list) and experience:
        if isinstance(experience[0], dict):
            return experience[0].get("company", "") or experience[0].get("title", "")

    return ""


def _build_candidate_signal(_name: str, field: str, skills: list, project: str, education: str) -> str:
    """Build a short, concrete signal about the candidate's ability."""
    parts = []
    if education:
        parts.append(education.split(",")[0].strip())  # just degree or school
    parts.append(f"focused on {field}")
    if project:
        parts.append(f"recently worked on {project}")
    elif skills:
        parts.append(f"building with {', '.join(skills[:2])}")
    return " ".join(parts) if parts else f"early-career {field} candidate"


# ── Stage 2: Lead Profile Extraction ───────────────────────────────────────────

def extract_lead_profile(lead: Lead) -> dict:
    """Extract a structured profile from lead data.

    Returns:
        dict with: lead_name, lead_role, company_name, company_focus,
                   department_hint, contextual_hook
    """
    lead_name = lead.name or "there"
    lead_role = lead.title or ""
    company_name = lead.company or ""
    industry = lead.industry or ""

    # Infer department from title
    department_hint = _infer_department(lead_role)

    # Use real company description from Apollo if available, else fall back to generated hook
    if lead.company_description:
        company_context = lead.company_description
    else:
        company_context = _build_contextual_hook(company_name, lead_role, industry, department_hint)

    # Build a "why this person" line tailored to their role
    why_this_person = _build_why_this_person(department_hint)

    return {
        "lead_name": lead_name,
        "lead_role": lead_role,
        "company_name": company_name,
        "company_focus": industry,
        "department_hint": department_hint,
        "contextual_hook": company_context,
        "why_this_person": why_this_person,
        "has_company_description": bool(lead.company_description),
    }


def _infer_department(title: str) -> str:
    """Infer department from job title."""
    title_lower = title.lower()
    if any(t in title_lower for t in ["engineer", "developer", "architect", "sre", "devops"]):
        return "engineering"
    if any(t in title_lower for t in ["data", "analyst", "ml", "ai", "scientist"]):
        return "data"
    if any(t in title_lower for t in ["product", "pm"]):
        return "product"
    if any(t in title_lower for t in ["design", "ux", "ui"]):
        return "design"
    if any(t in title_lower for t in ["hr", "recruit", "talent", "people"]):
        return "people"
    if any(t in title_lower for t in ["ceo", "cto", "founder", "vp", "director", "head", "chief"]):
        return "leadership"
    if any(t in title_lower for t in ["market", "growth", "brand"]):
        return "marketing"
    if any(t in title_lower for t in ["sales", "account", "business dev"]):
        return "sales"
    return "general"


def _build_why_this_person(department: str) -> str:
    """Build a short reason why the sender is emailing THIS specific person."""
    if department == "leadership":
        return "reaching out directly since you'd know the team better than a careers page would"
    if department == "engineering":
        return "figured you'd have more context on hiring than the recruiter would"
    if department == "people":
        return "saw you handle recruiting and wanted to reach out directly"
    if department == "data":
        return "thought you'd be the right person given you're on the data side"
    if department == "product":
        return "thought you'd be a good person to ask given your product role"
    if department == "marketing":
        return "thought you'd be the right person given your marketing background"
    if department == "sales":
        return "thought you might know who handles hiring on your side"
    return "thought you might know who the right person to connect with is"


def _build_contextual_hook(company: str, role: str, industry: str, department: str) -> str:
    """Build a natural-sounding hook about how the sender discovered this person.

    Must sound like a real person stumbling on the company, NOT flattery.
    """
    hooks = []
    if industry:
        hooks.append(f"came across {company} while looking at companies in the {industry} space")
        hooks.append(f"was reading about teams working in {industry} and noticed {company}")
        hooks.append(f"saw {company} mentioned in a few {industry} discussions recently")
    if company:
        hooks.append(f"noticed {company} has been growing and looked into the team")
        hooks.append(f"saw some interesting things about what {company} is working on")
    if department == "engineering":
        hooks.append(f"was looking at engineering teams in the area and came across {company}")
    if not hooks:
        hooks.append(f"came across {company} recently and wanted to reach out")

    return random.choice(hooks)


# ── Stage 3: Style Assignment ──────────────────────────────────────────────────

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


# ── Stage 4: Structured Email Generation ───────────────────────────────────────

def _build_generation_prompt(
    candidate_profile: dict,
    lead_profile: dict,
    style: str,
) -> str:
    """Build the structured prompt for email generation.

    The prompt enforces the 5-part email structure and all hard rules.
    """
    style_info = STYLE_DESCRIPTIONS.get(style, STYLE_DESCRIPTIONS["warm_intro"])
    greeting = random.choice(GREETINGS).replace("{name}", lead_profile["lead_name"].split()[0])
    closing = random.choice(CLOSINGS)
    signoff = random.choice(SIGNOFFS).replace("{name}", candidate_profile["candidate_name"].split()[0] if candidate_profile["candidate_name"] else "Me")

    # Vary sentence order
    order_seed = random.choice(["hook_first", "signal_first"])

    has_flex = candidate_profile.get("has_flex_notes", False)
    has_company_desc = lead_profile.get("has_company_description", False)
    signal_instruction = (
        "Use the SENDER SIGNAL verbatim as the basis — it's what they actually built and achieved, not a resume summary."
        if has_flex else
        "Use the SENDER SIGNAL as raw material — reference a specific skill or project, not generic phrases."
    )
    hook_instruction = (
        "Use the COMPANY CONTEXT as the basis for your hook — it describes what the company actually does."
        if has_company_desc else
        "Use the CONTEXTUAL HOOK as inspiration but rephrase it naturally in your own words."
    )

    prompt = f"""Write a short cold outreach email. You must follow EVERY rule below exactly.

SENDER: {candidate_profile['candidate_name']}
SENDER SIGNAL: {candidate_profile['short_candidate_signal']}
SENDER FIELD: {candidate_profile['primary_field']}
SENDER LOOKING FOR: {candidate_profile['job_interest']} roles
SENDER KEY SKILLS: {', '.join(candidate_profile['key_skills']) if candidate_profile['key_skills'] else candidate_profile['primary_field']}

RECIPIENT: {lead_profile['lead_name']}
RECIPIENT ROLE: {lead_profile['lead_role']} at {lead_profile['company_name']}
COMPANY CONTEXT: {lead_profile['contextual_hook']}
DEPARTMENT: {lead_profile['department_hint']}
WHY THIS PERSON: {lead_profile['why_this_person']}

STYLE: {style_info['tone']}
HOOK APPROACH: {style_info['hook_style']}
ASK APPROACH: {style_info['soft_ask']}

STRUCTURE (you must include all 5 parts in this order):
1. GREETING: Use exactly "{greeting}"
2. MICRO HOOK: One sentence about the company or what they do. {hook_instruction} {"Put this before the candidate signal." if order_seed == "hook_first" else "You may weave this into the candidate signal sentence."}
3. CANDIDATE SIGNAL: One sentence with a concrete signal from the sender's background. {signal_instruction}
4. RELEVANCE BRIDGE: One short sentence using WHY THIS PERSON — explain why you're emailing this specific person, not just the company.
5. SOFT ASK + CLOSING: End with "{closing}" or a similar casual line. Sign off with "{signoff}".

HARD RULES:
- Total body: 70-120 words. Count carefully.
- 3-5 sentences total (not counting greeting and signoff).
- Use simple, casual language. Write like a real person typing quickly.
- Subject line: lowercase, casual, under 40 chars. Like a text message. Examples: "quick question", "saw your team at {{company}}", "curious about {{company}}"
- Use \\n\\n between paragraphs. Keep to 2-3 short paragraphs max.

ABSOLUTELY FORBIDDEN (instant fail if any appear):
- Em dashes (the -- or \u2014 character)
- "I hope this email finds you well"
- "I am passionate about" or "excited to apply"
- "I believe my skills align"
- Any corporate or formal phrasing
- Praising or summarizing the company ("Your company's commitment to...")
- Starting any sentence with "As a..." or "With my experience in..."
- "I would be honored" or "contribute to your team"
- Bullet points or numbered lists
- Perfect grammar that sounds robotic. Use contractions naturally.

Return valid JSON only: {{"subject": "...", "body": "..."}}
Body must use \\n\\n between paragraphs."""

    return prompt


# ── Stage 5: Tone Cleaner ──────────────────────────────────────────────────────

def clean_tone(text: str) -> str:
    """Post-generation cleanup pass to remove AI signals and enforce rules.

    - Removes em dashes
    - Removes forbidden phrases
    - Simplifies overly formal sentences
    - Ensures contractions are used
    """
    # Remove em dashes and en dashes
    text = text.replace("\u2014", ",")  # em dash
    text = text.replace("\u2013", ",")  # en dash
    text = text.replace(" -- ", ", ")
    text = text.replace("--", ",")

    # Remove forbidden phrases (case-insensitive)
    for phrase in FORBIDDEN_PHRASES:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        text = pattern.sub("", text)

    # Expand formal constructions to contractions
    replacements = [
        ("I have been", "I've been"),
        ("I am ", "I'm "),
        ("I would ", "I'd "),
        ("I will ", "I'll "),
        ("it is ", "it's "),
        ("that is ", "that's "),
        ("do not ", "don't "),
        ("does not ", "doesn't "),
        ("cannot ", "can't "),
        ("would not ", "wouldn't "),
        ("will not ", "won't "),
        ("could not ", "couldn't "),
        ("should not ", "shouldn't "),
        ("they are ", "they're "),
        ("we are ", "we're "),
        ("you are ", "you're "),
        ("there is ", "there's "),
        ("who is ", "who's "),
        ("what is ", "what's "),
        ("let us ", "let's "),
    ]
    for formal, casual in replacements:
        text = re.sub(re.escape(formal), casual, text, flags=re.IGNORECASE)

    # Remove double spaces and clean up punctuation artifacts
    text = re.sub(r"  +", " ", text)
    text = re.sub(r" ,", ",", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\.\s*\.", ".", text)

    # Clean up any empty lines caused by removals
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ── Main Pipeline ──────────────────────────────────────────────────────────────

def generate_email_for_lead(lead: Lead, candidate: Candidate, style: str) -> Tuple[str, str]:
    """Generate a human-sounding outreach email through the structured pipeline.

    Pipeline stages:
      1. Extract candidate profile
      2. Extract lead profile
      3. Build structured prompt
      4. Generate via LLM
      5. Clean tone
      6. Validate

    Returns:
        Tuple of (subject, body) strings.
    """
    # Stage 1: Candidate profile extraction
    candidate_profile = extract_candidate_profile(candidate)

    # Stage 2: Lead profile extraction
    lead_profile = extract_lead_profile(lead)

    # Stage 3-4: Build prompt and generate
    prompt = _build_generation_prompt(candidate_profile, lead_profile, style)

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

        # Stage 5: Tone cleaner
        body = clean_tone(body)
        subject = clean_tone(subject)

        # Stage 6: Validation
        if not subject or len(subject) < 5:
            raise ValueError("Subject too short")
        if not body or len(body) < 30:
            raise ValueError("Body too short")

        # Word count check - log warning if outside range
        word_count = len(body.split())
        if word_count > 130:
            logger.warning("[EmailGen] Body too long (%d words) for %s, trimming", word_count, lead.name)
        elif word_count < 50:
            logger.warning("[EmailGen] Body too short (%d words) for %s", word_count, lead.name)

        logger.info("[EmailGen] Generated for %s: subject='%s' (%d words)",
                    lead.name, subject[:50], word_count)
        return subject, body

    except Exception as e:
        logger.error("[EmailGen] Failed for %s: %s", lead.name, e, exc_info=True)
        raise ValueError(f"Email generation failed for {lead.name}: {e}")
