"""Email Generator Service — Structured pipeline for human-sounding outreach.

Generates emails through a multi-stage pipeline:
  candidate_profile_extraction -> lead_profile_extraction -> template_selection
  -> structured_email_generation -> tone_cleaner -> final_email

Each style has a distinct email skeleton — not just a tone change.
Hard rules: no em dashes, no AI phrasing, no copy-pasted resume language.
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

# Per-style email skeletons — each style has a genuinely different structure, not just a tone change.
# Variables filled via .format() in _build_generation_prompt().
STYLE_STRUCTURES = {
    "warm_intro": (
        "STRUCTURE:\n"
        "1. GREETING: Use exactly \"{greeting}\"\n"
        "2. One casual observational sentence about {company} or {lead_name}'s team. "
        "Genuine personal discovery — not flattery, not a company summary. {hook_instruction}\n"
        "3. One understated sentence about your background. Mention your work briefly. "
        "Don't open with it. {signal_instruction}\n"
        "4. Close with this exact question: \"{why_this_person}\" Then sign off with \"{signoff}\". "
        "No separate closing pleasantry needed.\n\n"
        "Word target: 75-95 words. Humble, warm, conversational. Company hook comes before credentials."
    ),
    "value_prop": (
        "STRUCTURE:\n"
        "1. GREETING: Use exactly \"{greeting}\"\n"
        "2. Lead with YOUR work — this is the FIRST sentence after the greeting. "
        "State the concrete thing you built and its outcome metric. {signal_instruction}\n"
        "3. Connect your work to what {company} does in one sentence. "
        "Why does your background matter to them specifically? {hook_instruction}\n"
        "4. Close with this exact question: \"{why_this_person}\" Then sign off with \"{signoff}\".\n\n"
        "Word target: 85-110 words. Specific, confident. Your work leads — company hook is the bridge."
    ),
    "company_curiosity": (
        "STRUCTURE:\n"
        "1. GREETING: Use exactly \"{greeting}\"\n"
        "2. Spend 2 sentences on what {company} is doing or building. "
        "This is 60-70% of the email. Genuine curiosity about their work — "
        "not a compliment about the company. {hook_instruction}\n"
        "3. One brief sentence about yourself: what you've built or your field. Keep it minimal.\n"
        "4. Close with this exact question: \"{why_this_person}\" Then sign off with \"{signoff}\".\n\n"
        "Word target: 80-100 words. The candidate barely features — this email is mostly about their work."
    ),
    "peer_to_peer": (
        "STRUCTURE:\n"
        "1. GREETING: Use exactly \"{greeting}\"\n"
        "2. Open with a collegial observation about their role, their team's work, "
        "or a tool/problem in the space. Write like a Slack DM, not a job application. {hook_instruction}\n"
        "3. Introduce yourself briefly as a peer — what you work on. One sentence. "
        "Do NOT say you're looking for a job or opportunities.\n"
        "4. Close with this exact question: \"{why_this_person}\" Then sign off with \"{signoff}\".\n\n"
        "Word target: 65-85 words. Zero job-seeker language. Peer-to-peer only."
    ),
    "direct_ask": (
        "STRUCTURE:\n"
        "1. GREETING: Use exactly \"{greeting}\"\n"
        "2. One sentence only: what you built + key impact metric. "
        "Start with 'I built' or 'I worked on'. {signal_instruction}\n"
        "3. One sentence: why you're reaching out to {lead_name} at {company} specifically.\n"
        "4. Close with this exact question: \"{why_this_person}\" Then sign off with \"{signoff}\".\n\n"
        "Word target: 50-70 words ONLY. Deliberately short. Busy people appreciate brevity."
    ),
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

def extract_candidate_profile(candidate: Candidate, fallback_name: str = "") -> dict:
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

    name = personal.get("name") or parsed.get("name") or fallback_name or ""

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
        # Normalize: if signal starts with a bare past-tense verb (no subject),
        # prepend "I" so the LLM reads it as an action the person did, not their identity.
        _verb_starts = {
            "built", "created", "developed", "designed", "launched", "led",
            "worked", "shipped", "wrote", "managed", "ran", "grew", "reduced",
            "increased", "automated", "scaled", "migrated", "deployed",
            "architected", "implemented", "owned", "drove", "delivered",
            "helped", "rebuilt", "spearheaded", "streamlined", "optimized",
        }
        first_word = signal.split()[0].lower().rstrip(",.")
        if first_word in _verb_starts:
            signal = "I " + signal[0].lower() + signal[1:]
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

    # Use real company description from Apollo if available and substantive (>50 chars)
    # Short descriptions like "Software company" add no value — use size-aware fallback instead
    company_size = lead.company_size or ""
    if lead.company_description and len(lead.company_description.strip()) > 50:
        company_context = lead.company_description.strip()
    else:
        company_context = _build_contextual_hook(company_name, lead_role, industry, department_hint, company_size)

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
        "has_company_description": bool(lead.company_description and len(lead.company_description.strip()) > 50),
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
    """Build the closing ask question — direct and natural, no preamble reasoning."""
    if department == "people":
        return "Would you be open to a quick chat, or know who's the right person to loop in?"
    if department == "data":
        return "Would you know if there's an opening on the data side, or who I should reach out to?"
    if department == "product":
        return "Would you know if there's an opening, or who on the product side to talk to?"
    if department == "sales":
        return "Would you know if there's an opening, or who handles that on your side?"
    # engineering, leadership, marketing, design, general
    return "Would you know if there's an opening, or who I should reach out to?"


def _build_contextual_hook(company: str, role: str, industry: str, department: str, company_size: str = "") -> str:
    """Build a natural-sounding hook about how the sender discovered this person.

    Uses company size to write more targeted hooks — small company hooks feel
    different from large-company hooks. Must sound like a real person, not flattery.
    """
    size = company_size.lower() if company_size else ""
    is_small = any(s in size for s in ["1-10", "11-50"])
    is_mid = any(s in size for s in ["51-200", "201-500", "201-1000"])
    is_large = any(s in size for s in ["1001", "5001", "10001"])

    hooks = []

    if is_small and company:
        hooks.append(f"noticed {company} came up when I was looking at smaller teams in {industry or 'the space'}")
        hooks.append(f"saw {company} is a small team and wanted to reach out before you've fully built out")
        if industry:
            hooks.append(f"was looking at early-stage {industry} teams and came across {company}")
    elif is_large and company:
        hooks.append(f"saw {company}'s {department} team and wanted to reach out directly")
        hooks.append(f"noticed {company} has a large {department} org and wanted to find the right person")
        if industry:
            hooks.append(f"came across {company} while looking at established {industry} companies")
    elif is_mid and company:
        hooks.append(f"noticed {company} has been growing and looked into the team")
        if industry:
            hooks.append(f"saw {company} while researching {industry} companies at your scale")

    # Fallbacks when size unknown or nothing above matched
    if not hooks:
        if industry:
            hooks.append(f"came across {company} while looking at {industry} companies")
            hooks.append(f"saw {company} come up a few times when researching {industry} teams")
        if company:
            hooks.append(f"noticed {company} came up when I was researching companies in the space")
        if department == "engineering":
            hooks.append(f"was looking at engineering teams and {company} caught my attention")
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

    Each style gets its own structural skeleton — not just a tone change.
    """
    greeting = random.choice(GREETINGS).replace("{name}", lead_profile["lead_name"].split()[0])
    closing = random.choice(CLOSINGS)
    signoff = random.choice(SIGNOFFS).replace(
        "{name}", candidate_profile["candidate_name"].split()[0] if candidate_profile["candidate_name"] else "Me"
    )

    has_flex = candidate_profile.get("has_flex_notes", False)
    has_company_desc = lead_profile.get("has_company_description", False)

    signal_instruction = (
        "SENDER SIGNAL describes something the sender BUILT or DID — it is NOT their job title. "
        "Frame it as 'I built...' or 'I worked on...' — never as identity ('I am a...'). "
        "Use it as the basis for one concrete sentence about their work and impact."
        if has_flex else
        "Reference a specific skill or project from SENDER SIGNAL — not generic phrases like 'background in X'."
    )
    hook_instruction = (
        "Use the COMPANY CONTEXT as the basis — it's what the company actually does. "
        "Show you know what they're about, don't just name-drop the company."
        if has_company_desc else
        "Rephrase the COMPANY CONTEXT naturally — make it sound like something you personally noticed, "
        "not a templated observation. Be specific to the company, not just the industry."
    )

    # Build the per-style structural template
    structure_tmpl = STYLE_STRUCTURES.get(style, STYLE_STRUCTURES["warm_intro"])
    structure = structure_tmpl.format(
        greeting=greeting,
        closing=closing,
        signoff=signoff,
        hook_instruction=hook_instruction,
        signal_instruction=signal_instruction,
        why_this_person=lead_profile["why_this_person"],
        company=lead_profile["company_name"],
        lead_role=lead_profile["lead_role"],
        lead_name=lead_profile["lead_name"].split()[0],
        job_interest=candidate_profile["job_interest"],
    )

    # Synthesis note — make the connection between signal and company explicit
    synthesis = (
        "SYNTHESIS: Don't just list your background and then ask. "
        "Make the connection: why would someone who built what you built be valuable to a company that does what "
        f"{lead_profile['company_name']} does? One sentence that links your work to their context is worth more "
        "than two sentences that don't connect. The email must feel written specifically for this company."
    )
    if has_company_desc:
        synthesis += (
            f" You have real context about what {lead_profile['company_name']} does — use it to show you did "
            "your homework, not just to prove you know what they do."
        )

    prompt = f"""Write a short cold outreach email. Follow EVERY rule below exactly.

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

{structure}

{synthesis}

HARD RULES:
- Use simple, casual language. Write like a real person typing quickly, not a template.
- Subject line: lowercase, casual, under 40 chars. Like a text message subject.
- Use \\n\\n between paragraphs. 2-3 short paragraphs max.
- SENDER SIGNAL is something the sender BUILT or ACHIEVED — never interpret it as their job title.

ABSOLUTELY FORBIDDEN:
- Em dashes (-- or \u2014)
- "I hope this email finds you well"
- "I am passionate about" / "excited to apply" / "I believe my skills align"
- Corporate phrasing, flattery, praising the company
- Starting any sentence with "As a..." or "With my experience in..."
- "I would be honored" / "contribute to your team" / "make a meaningful impact"
- Bullet points or numbered lists
- "I am a [thing from the signal]" — the signal is a project, not an identity

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

def generate_email_for_lead(lead: Lead, candidate: Candidate, style: str, user_name: str = "") -> Tuple[str, str]:
    """Generate a human-sounding outreach email through the structured pipeline.

    Pipeline stages:
      1. Extract candidate profile
      2. Extract lead profile
      3. Build structured prompt
      4. Generate via LLM
      5. Clean tone
      6. Validate

    Args:
        user_name: The sender's display name (e.g. from User.name). Used as
                   fallback when the resume parser doesn't extract a name,
                   preventing the sign-off from defaulting to "Me".

    Returns:
        Tuple of (subject, body) strings.
    """
    # Stage 1: Candidate profile extraction
    candidate_profile = extract_candidate_profile(candidate, fallback_name=user_name)

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
