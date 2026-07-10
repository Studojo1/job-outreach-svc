"""Email Generator Service — Structured pipeline for human-sounding outreach.

Generates emails through a multi-stage pipeline:
  candidate_profile_extraction -> lead_profile_extraction -> band_inference
  -> structured_email_generation -> tone_cleaner -> final_email

There is ONE email shape. The lead's team-size band (solo/small/structured) sets
voice and altitude only — it never changes the structure. The band is inferred
from company size; the student never sees or picks it.

Hard rules: no em dashes, no AI phrasing, no copy-pasted resume language.
"""

import random
import re
from typing import Tuple, Dict

from core.config import settings
from core.logger import get_logger
from database.models import Lead, Candidate
from services.shared.ai.azure_openai_client import generate_json, ContentFilterError

logger = get_logger(__name__)

# ── Team-size bands ────────────────────────────────────────────────────────────
# The ONLY per-lead variable. Sets voice and altitude; never structure.
# Inferred from company size — the student never sees or picks this.

BAND_SOLO = "solo"
BAND_SMALL = "small"
BAND_STRUCTURED = "structured"

BAND_VOICE = {
    BAND_SOLO: (
        "VOICE: They are the whole company, or close to it. Write to a person who "
        "makes every call themselves and reads their own inbox. Direct, unceremonious, "
        "zero process language. No 'your team' — it's them."
    ),
    BAND_SMALL: (
        "VOICE: A small team where the recipient still touches the work. "
        "Collegial and concrete. They care about what got built, not credentials."
    ),
    BAND_STRUCTURED: (
        "VOICE: An established org with process. The recipient is one node in a larger "
        "system and is likely to route you rather than decide. Be brief and easy to forward. "
        "Respect their time; do not assume they own hiring."
    ),
}


def infer_band(lead: Lead) -> str:
    """Infer the team-size band from company size. System-only, never user-facing."""
    size = (lead.company_size or "").strip()
    if size in ("1-10",):
        return BAND_SOLO
    if size in ("11-50", "51-200"):
        return BAND_SMALL
    if size:
        return BAND_STRUCTURED
    # Unknown size: 'small' is the safest middle — reads fine to both a founder
    # and a manager, whereas 'solo' misfires badly at a 5000-person company.
    return BAND_SMALL


# The single email shape. One structure for every lead, in every band.
# Slot 2 (the synthesis line) is filled by deep lead research when it clears the
# depth guard; otherwise it is omitted entirely and the email drops to the bare ask.
EMAIL_SHAPE = (
    "STRUCTURE:\n"
    "1. GREETING: Use exactly \"{greeting}\"\n"
    "{body_instruction}\n"
    "{final_slot}. Close with this exact question: \"{why_this_person}\" "
    "Then sign off with \"{signoff}\".\n\n"
    "{word_target}"
)

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

# ── System prompt (sent as system role to avoid Azure jailbreak false positives) ─

_EMAIL_SYSTEM_PROMPT = (
    "You are a ghostwriter writing short cold outreach emails for early-career job seekers.\n\n"
    "HARD RULES:\n"
    "- Use simple, casual language. Write like a real person typing quickly, not a template.\n"
    "- Subject line: lowercase, casual, under 40 chars. Like a text message subject.\n"
    "- Use \\n\\n between paragraphs. 2-3 short paragraphs max.\n"
    "- TRUTH ONLY: Use ONLY the facts given in the profile. NEVER invent, embellish, or add "
    "specifics. Do not make up projects, tools, employers, results, metrics, numbers, percentages, "
    "or timeframes. If a detail is not explicitly provided, do not state it.\n"
    "- SENDER SIGNAL, if provided, is something the sender genuinely BUILT or ACHIEVED — never "
    "interpret it as their job title. If NO concrete project/achievement is provided, do NOT invent "
    "one: write a short, honest note based only on the sender's real field, listed skills, and what "
    "they're looking for. It is fine for the email to be simple and modest.\n"
    "- Write the sender's name EXACTLY as given. Do not shorten, expand, abbreviate, or alter it.\n"
    "- SENDER CITY is optional context. ONLY weave it in if it feels natural (e.g. lead's company is in "
    "same city). Never force it. If unsure, omit it entirely.\n\n"
    "ABSOLUTELY FORBIDDEN:\n"
    "- Em dashes (-- or —)\n"
    "- Inventing any project, tool, result, employer, experience, or skill not in the profile\n"
    '- Any specific number, percentage, metric, or timeframe not explicitly provided (e.g. "cut time by 40%")\n'
    '- Claiming the sender "built", "created", "shipped", or "launched" anything unless it is explicitly in the signal\n'
    '- "I hope this email finds you well"\n'
    '- "I am passionate about" / "excited to apply" / "I believe my skills align"\n'
    "- Corporate phrasing, flattery, praising the company\n"
    '- Starting any sentence with "As a..." or "With my experience in..."\n'
    '- "I would be honored" / "contribute to your team" / "make a meaningful impact"\n'
    "- Bullet points or numbered lists\n"
    '- "I am a [thing from the signal]" — the signal is a project, not an identity'
)

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
    # Round-2 additions: the exact generic patterns observed in user testing.
    "fascinating work",
    "intersection of ai",
    "intersection of AI",
    "ai-driven solutions that scale",
    "AI-driven solutions that scale",
    "doing some fascinating",
    "some fascinating work",
    "doing exciting work",
    "looks like your team is doing",
    "your team is doing some",
    "while researching companies",
    "while researching AI",
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

    _DEGREE_WORDS = {"bachelor", "master", "phd", "doctorate", "mba", "bsc", "msc", "b.tech", "m.tech", "b.e", "m.e", "associate"}
    # Resume parsers often grab a header/title instead of the name (e.g. "AIML STUDENT").
    # Reject those so we fall back to the authenticated account name.
    _NON_NAME_WORDS = {"student", "resume", "cv", "curriculum", "vitae", "fresher", "aiml", "profile", "objective"}
    def _is_valid_name(n: str) -> bool:
        if not n:
            return False
        low = n.lower()
        if any(w in low for w in _DEGREE_WORDS):
            return False
        words = low.split()
        if any(w in _NON_NAME_WORDS for w in words):
            return False
        # All-caps multi-word strings are almost always resume section headers, not names.
        if n.isupper() and len(words) >= 2:
            return False
        return True

    raw_name = personal.get("name") or parsed.get("name") or ""
    name = raw_name if _is_valid_name(raw_name) else (fallback_name or "")

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

    # Candidate city — used as optional context in the email opener
    # (so emails to leads in the same city can naturally reference it).
    candidate_city = ""
    locs = prefs.get("locations") or []
    if isinstance(locs, list) and locs:
        candidate_city = str(locs[0]).strip()
    if not candidate_city:
        # fallback to resume_profile geography city if present
        rp = candidate.resume_profile if isinstance(candidate.resume_profile, dict) else {}
        candidate_city = (rp.get("geography") or {}).get("city", "") or ""

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

    # Q3 answer: the one choice or trick that made the project work. Carried as
    # synthesis match-material for the research join — never emitted as a leading
    # SENDER field in the prompt.
    work_principle = flex.get("work_principle", "").strip()
    # Optional credibility marker the user wants in every email (e.g. school /
    # recent-grad status). Only set when flex_notes.credential exists, so this is
    # opt-in per candidate and does not change behaviour for everyone else.
    credential = flex.get("credential", "").strip()

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
        "candidate_city": candidate_city,
        "work_principle": work_principle,
        "credential": credential,
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

    Pulls the rich `CompanyProfile.extracted_facts` blob (round-2 enrichment)
    when available — that's the structured output of the per-company fact
    extractor LLM (what_they_build, core_tech, primary_market, hiring_signal,
    recent_momentum). When available, the email prompt cites these facts
    instead of guessing from the company name.

    Returns:
        dict with: lead_name, lead_role, company_name, company_focus,
                   department_hint, contextual_hook, company_facts,
                   recent_job_postings, what_they_build, core_tech,
                   recent_momentum, hiring_signal
    """
    lead_name = lead.name or "there"
    lead_role = lead.title or ""
    company_name = lead.company or ""
    industry = lead.industry or ""

    # Infer department from title
    department_hint = _infer_department(lead_role)

    # ── Look up the round-2 CompanyProfile if we have a domain.
    company_profile = _lookup_company_profile(lead.company_domain)
    facts = (company_profile.extracted_facts if company_profile else None) or {}

    # Build the strongest "what does this company do" string we have.
    # Priority: extracted_facts.what_they_build → CompanyProfile.short_description
    # → CompanyProfile.website_summary → Apollo's lead.company_description
    # → size-aware contextual fallback.
    company_context_parts = []
    if facts.get("what_they_build"):
        company_context_parts.append(facts["what_they_build"])
    if facts.get("primary_market"):
        company_context_parts.append(f"primary market: {facts['primary_market']}")
    if facts.get("recent_momentum"):
        company_context_parts.append(facts["recent_momentum"])

    company_size = lead.company_size or ""
    if company_context_parts:
        company_context = " | ".join(company_context_parts)
        has_company_description = True
    elif company_profile and company_profile.short_description and len(company_profile.short_description) > 50:
        company_context = company_profile.short_description.strip()
        has_company_description = True
    elif lead.company_description and len(lead.company_description.strip()) > 50:
        company_context = lead.company_description.strip()
        has_company_description = True
    else:
        company_context = _build_contextual_hook(company_name, lead_role, industry, department_hint, company_size)
        has_company_description = False

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
        "has_company_description": has_company_description,
        # ── Round-2 enrichment fields piped into the prompt builder.
        "what_they_build": facts.get("what_they_build"),
        "core_tech": facts.get("core_tech") or [],
        "primary_market": facts.get("primary_market"),
        "recent_momentum": facts.get("recent_momentum"),
        "hiring_signal": facts.get("hiring_signal"),
        "recent_job_postings": (company_profile.recent_job_postings or []) if company_profile else [],
    }


def _lookup_company_profile(domain: str | None):
    """Best-effort fetch of CompanyProfile by domain. Opens its own short-lived
    DB session so callers don't need to thread one through. Returns None on
    any failure — email generation must always work even without enrichment."""
    if not domain:
        return None
    try:
        from database.session import SessionLocal
        from database.models import CompanyProfile
    except Exception:
        return None
    try:
        db = SessionLocal()
        try:
            return db.query(CompanyProfile).filter_by(domain=domain.lower()).first()
        finally:
            db.close()
    except Exception as e:
        logger.warning("[EmailGen] CompanyProfile lookup failed for %s: %s", domain, e)
        return None


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


# ── Stage 3: Band Assignment ───────────────────────────────────────────────────

def assign_style(lead: Lead, selected_styles: list | None = None) -> str:
    """Deprecated name, kept so existing call sites keep working.

    The style system is retired: there is one email shape. This now returns the
    inferred team-size band, which is what gets persisted to
    EmailSent.assigned_style and read back at generation time.
    `selected_styles` is ignored.
    """
    band = infer_band(lead)
    logger.info("[EmailGen] Band '%s' for %s at %s (size=%s)",
                band, lead.name, lead.company, lead.company_size or "unknown")
    return band


# ── Stage 4: Structured Email Generation ───────────────────────────────────────

def _build_generation_prompt(
    candidate_profile: dict,
    lead_profile: dict,
    band: str,
    research: dict | None = None,
) -> str:
    """Build the prompt for the single email shape.

    `band` sets voice and altitude only. `research` carries the deep per-lead
    findings that a synthesis line is built from; when it is None (no research,
    or the depth guard rejected it) the email drops to the bare ask.
    """
    greeting = random.choice(GREETINGS).replace("{name}", ((lead_profile["lead_name"] or "").split() or ["there"])[0])
    signoff = random.choice(SIGNOFFS).replace(
        "{name}", ((candidate_profile["candidate_name"] or "").split() or ["Me"])[0]
    )

    has_flex = candidate_profile.get("has_flex_notes", False)

    signal_instruction = (
        "SENDER SIGNAL describes something the sender BUILT or DID — it is NOT their job title. "
        "Frame it as 'I built...' or 'I worked on...' — never as identity ('I am a...'). "
        "Use it as the basis for one concrete sentence about their work and impact."
        if has_flex else
        "Reference a specific skill or project from SENDER SIGNAL — not generic phrases like 'background in X'."
    )

    if research:
        # Synthesis line: names the lead's specific thing, joined to one true,
        # smaller claim from the sender. Research already cleared the depth guard.
        body_instruction = (
            "2. SYNTHESIS LINE. Open with the recipient: restate ABOUT THE RECIPIENT "
            "in your own words. Everything in that field is about THEM, never about the "
            "sender. Then join it to one true, SMALLER claim about the sender's own work. "
            "Never attribute the sender's method, project, or metric to the recipient, and "
            "never compliment them on something the sender did.\n"
            f"3. One understated sentence grounding the sender's work. {signal_instruction}"
        )
        final_slot = "4"
        word_target = "Word target: 70-95 words."
        guard = (
            "\n\nFORBIDDEN IN THE SYNTHESIS LINE:\n"
            "- Any claim of equivalence ('same bet', 'exactly what I did', 'we both')\n"
            "- Any hedge ('feels relevant', 'seems similar', 'might align', 'resonates')\n"
            "- Praising the person or the company\n"
            "- Restating their point back to them without adding the sender's own claim"
        )
    else:
        # No research cleared the guard. A shallow observation is worse than none:
        # send the bare ask rather than a line that would fit anyone in this role.
        body_instruction = (
            f"2. One understated sentence about the sender's work. {signal_instruction} "
            "Do NOT open with an observation about the company or the recipient. "
            "You have no specific, verified fact about this person, and a generic "
            "observation is worse than none."
        )
        final_slot = "3"
        word_target = "Word target: 45-65 words ONLY. Deliberately short."
        guard = (
            "\n\nYou have NO researched fact about this recipient. Therefore:\n"
            "- Do NOT open with 'I came across...', 'I noticed...', 'saw that...'\n"
            "- Do NOT characterise their work, their team, or their company\n"
            "- Do NOT use the COMPANY CONTEXT as an opener; it is background only\n"
            "Go straight from the greeting to the sender's own work, then the ask."
        )

    structure = EMAIL_SHAPE.format(
        greeting=greeting,
        body_instruction=body_instruction,
        final_slot=final_slot,
        why_this_person=lead_profile["why_this_person"],
        signoff=signoff,
        word_target=word_target,
    )

    synthesis = BAND_VOICE.get(band, BAND_VOICE[BAND_SMALL]) + guard

    research_section = ""
    if research:
        rlines = []
        if research.get("quote"):
            rlines.append(f"THEIR WORDS (verbatim): \"{research['quote']}\"")
        if research.get("belief"):
            # This is the RECIPIENT-ONLY clause from the depth guard. It must never
            # contain anything the sender did: the writer treats it as a fact about
            # the recipient, so a fused line would credit them with the sender's work.
            rlines.append(f"ABOUT THE RECIPIENT (verified; use as the opener): {research['belief']}")
        if research.get("move"):
            rlines.append(f"A LIVE MOVE (light texture only): {research['move']}")
        if research.get("source_url"):
            rlines.append(f"SOURCE: {research['source_url']}")
        if rlines:
            research_section = "\n\nRESEARCH ON THIS SPECIFIC PERSON:\n" + "\n".join(rlines)

    candidate_city = candidate_profile.get("candidate_city") or ""
    city_line = f"\nSENDER CITY: {candidate_city}" if candidate_city else ""
    credential = candidate_profile.get("credential") or ""
    credential_line = (
        f"\nSENDER CREDENTIAL (MANDATORY, must appear in this email): {credential}. "
        f"The email is INVALID if it does not state that the sender is {credential}. "
        "Put it in the sender's first 'about me' sentence, blended with the signal "
        f"(e.g. \"I recently finished my Master's at HEC Paris and ...\" style for '{credential}'). "
        "Never omit it. Do not start a sentence with 'As a'."
        if credential else ""
    )

    # Company facts are BACKGROUND ONLY. They are identical for every lead at this
    # company, so they can never carry the synthesis line — a line built from them
    # would survive being sent to a different person in the same role, which is
    # exactly what the depth guard rejects. Industry is omitted entirely: it earns
    # near-zero weight and only tints what kind of proof fits.
    facts_block = []
    if lead_profile.get("what_they_build"):
        facts_block.append(f"WHAT THEY BUILD: {lead_profile['what_they_build']}")
    if lead_profile.get("core_tech"):
        facts_block.append(f"CORE TECH STACK: {', '.join(lead_profile['core_tech'])}")
    if lead_profile.get("recent_momentum"):
        facts_block.append(f"RECENT NEWS / MOMENTUM: {lead_profile['recent_momentum']}")
    facts_section = (
        "\n\nCOMPANY BACKGROUND (context for you; NOT a hook, do not open with it):\n"
        + "\n".join(facts_block)
    ) if facts_block else ""

    prompt = f"""Write a short cold outreach email. Follow the STRUCTURE exactly.

SENDER: {candidate_profile['candidate_name']}
SENDER SIGNAL: {candidate_profile['short_candidate_signal']}
SENDER FIELD: {candidate_profile['primary_field']}
SENDER LOOKING FOR: {candidate_profile['job_interest']} roles
SENDER KEY SKILLS: {', '.join(candidate_profile['key_skills']) if candidate_profile['key_skills'] else candidate_profile['primary_field']}{credential_line}{city_line}

RECIPIENT: {lead_profile['lead_name']}
RECIPIENT ROLE: {lead_profile['lead_role']} at {lead_profile['company_name']}
WHY THIS PERSON: {lead_profile['why_this_person']}{research_section}{facts_section}

{structure}

{synthesis}"""

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

def generate_email_for_lead(
    lead: Lead,
    candidate: Candidate,
    band: str = "",
    user_name: str = "",
    research: dict | None = None,
) -> Tuple[str, str]:
    """Generate a human-sounding outreach email in the single email shape.

    Pipeline stages:
      1. Extract candidate profile
      2. Extract lead profile
      3. Resolve team-size band (voice/altitude only)
      4. Build prompt and generate via LLM
      5. Clean tone
      6. Validate

    Args:
        band: Team-size band. Falls back to inference when not supplied.
        user_name: The sender's display name (e.g. from User.name). Used as
                   fallback when the resume parser doesn't extract a name,
                   preventing the sign-off from defaulting to "Me".
        research: Deep per-lead findings that cleared the depth guard. When None,
                  the email drops to the bare ask.

    Returns:
        Tuple of (subject, body) strings.
    """
    # Stage 1: Candidate profile extraction
    candidate_profile = extract_candidate_profile(candidate, fallback_name=user_name)

    # Stage 2: Lead profile extraction
    lead_profile = extract_lead_profile(lead)

    # Stage 3: Band. Older rows persisted a style name here; re-infer in that case.
    if band not in (BAND_SOLO, BAND_SMALL, BAND_STRUCTURED):
        band = infer_band(lead)

    # Stage 4: Build prompt and generate
    prompt = _build_generation_prompt(candidate_profile, lead_profile, band, research=research)

    schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "maxLength": 100},
            "body": {"type": "string", "maxLength": 2000},
        },
        "required": ["subject", "body"],
    }

    try:
        logger.info("[EmailGen] Generating for %s (%s) at %s, band=%s, research=%s",
                    lead.name, lead.title, lead.company, band, bool(research))

        cred = candidate_profile.get("credential") or ""
        # Acronym tokens (e.g. 'HEC') let us verify the credential actually landed
        # in the body; if the model dropped it, regenerate once. No credential or
        # no acronym -> single pass, unchanged behaviour.
        cred_required = [w.strip("(),.'") for w in cred.split() if w.isupper() and len(w.strip("(),.'")) >= 3]

        body = ""
        for attempt in range(2):
            result = generate_json(
                prompt, schema, temperature=0.85, system_prompt=_EMAIL_SYSTEM_PROMPT,
                deployment=settings.AZURE_OPENAI_EMAIL_DEPLOYMENT,
            )
            body = clean_tone(result.get("body", "").strip())  # Stage 5: tone cleaner
            if not cred_required or any(t.lower() in body.lower() for t in cred_required):
                break
            logger.info("[EmailGen] credential %s missing for %s, regenerating", cred_required, lead.name)

        # Subject: always "quick question {first name}" — ignore LLM output
        first_name = (lead.name or "").split()[0] if lead.name else ""
        subject = f"quick question {first_name}".strip()

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

    except ContentFilterError:
        raise  # let campaign_worker handle content filter blocks specifically
    except Exception as e:
        logger.error("[EmailGen] Failed for %s: %s", lead.name, e, exc_info=True)
        raise ValueError(f"Email generation failed for {lead.name}: {e}")


# ── Follow-up Email Generation ────────────────────────────────────────────────

def _extract_unused_project(candidate: Candidate, parent_body: str) -> Dict[str, str] | None:
    """Pick one project from the candidate's resume that wasn't mentioned in the original email.

    Returns None when the candidate has no real project to draw on. Callers must
    fall back to a bare nudge rather than inventing one: a fabricated project is a
    false claim made in the student's name.
    """
    body_lower = (parent_body or "").lower()

    projects = []
    parsed = candidate.parsed_json or {}
    resume_profile = candidate.resume_profile or {}

    for source in (resume_profile.get("projects"), parsed.get("projects")):
        if isinstance(source, list):
            for p in source:
                if isinstance(p, dict):
                    name = p.get("name") or p.get("title") or ""
                    desc = p.get("description") or p.get("summary") or ""
                    if name and desc:
                        projects.append({"name": str(name).strip(), "description": str(desc).strip()})

    for p in projects:
        name_words = [w for w in p["name"].lower().split() if len(w) > 3]
        if not any(w in body_lower for w in name_words):
            return p

    if projects:
        return projects[0]

    # Fall back to flex_notes.best_project before giving up.
    flex = candidate.flex_notes or {}
    if flex.get("best_project"):
        desc = flex["best_project"].strip()
        if flex.get("outcome"):
            desc += ". " + flex["outcome"].strip()
        return {"name": "project", "description": desc}

    return None


def generate_followup_email(lead: Lead, candidate: Candidate, parent_body: str, followup_number: int) -> str:
    """Generate a follow-up email body.

    followup_number=1 → Touch 2 (Day 5 bump): introduces one new project from the resume.
    followup_number=2 → Touch 3 (Day 12 exit): short graceful close, no pitch.

    Returns just the body (greeting + body + signoff). Subject inherits from parent.
    """
    candidate_name = "there"
    parsed = candidate.parsed_json or {}
    resume_profile = candidate.resume_profile or {}
    for source in (resume_profile, parsed.get("personal_info", {}), parsed):
        n = source.get("name") if isinstance(source, dict) else None
        if n and isinstance(n, str) and " " in n:
            candidate_name = n
            break

    first_name_candidate = candidate_name.split()[0] if candidate_name else "there"
    lead_first = (lead.name or "").split()[0] if lead.name else "there"
    lead_company = lead.company or ""
    lead_title = lead.title or ""

    linkedin_url = (candidate.flex_notes or {}).get("linkedin_url", "")

    schema = {
        "type": "object",
        "properties": {"body": {"type": "string"}},
        "required": ["body"],
    }

    if followup_number == 1:
        project = _extract_unused_project(candidate, parent_body)

        linkedin_line = (
            f'\nAfter the final sentence, add one line on its own: "Here\'s my LinkedIn if you\'d like to connect: {linkedin_url}" — output this exactly.'
            if linkedin_url else ""
        )

        if project is None:
            # No real project left to cite. Send a bare nudge rather than invent one.
            prompt = f"""Ghostwrite a 2-sentence follow-up email for {candidate_name}, a student.

Recipient: {lead_first}, {lead_title} at {lead_company}. This bumps an earlier email that got no reply.

Write the email body ONLY (greeting + 2 sentences + sign-off). Exactly 2 sentences:

Sentence 1: Bump the thread in one casual line. Use something like "just bumping this up" or "wanted to resurface this". Do NOT say "following up on my previous note" or "just checking in".

Sentence 2: Use this exact sentence: "Would you know if there's an opening, or who I should reach out to?"{linkedin_line}

Do NOT mention any project, skill, coursework, or achievement. You have no facts about this person's work. Inventing one is forbidden.

Format:
- Start with "Hi {lead_first},"
- Sign off: "{first_name_candidate}" on its own line, nothing else
- Body total under 35 words"""

            result = generate_json(
                prompt, schema, temperature=0.85, system_prompt=_EMAIL_SYSTEM_PROMPT,
                deployment=settings.AZURE_OPENAI_EMAIL_DEPLOYMENT,
            )
            body = clean_tone((result.get("body") or "").strip())
            if not body or len(body) < 20:
                raise ValueError(f"Follow-up body too short for {lead.name}")
            logger.info("[Followup] Generated bare-nudge touch 2 for %s (no project available)", lead.name)
            return body

        prompt = f"""Ghostwriting a short follow-up email for {candidate_name}, a student looking for internship roles.

The original email already introduced one project. This follow-up should feel like a quick human bump in the thread, not a sales email.

ORIGINAL EMAIL (already sent, do NOT reference or repeat anything from it):
---
{parent_body}
---

ONE NEW PROJECT to mention (was NOT in the original email above):
Name: {project['name']}
What it was: {project['description']}

Lead: {lead_first}, {lead_title} at {lead_company}

Write the email body ONLY (greeting + 3 sentences + sign-off). Exactly 3 sentences between the greeting and sign-off:

Sentence 1: Bump the thread in one casual line. Use something like "just bumping this up" or "wanted to resurface this". Do NOT say "following up on my previous note" or "just checking in".

Sentence 2: Introduce the project above in one punchy sentence. State what it was and one concrete thing from it (a deliverable, outcome, or specific feature). Do NOT say "which might align" or "could be relevant" or "I recently". Just state it as a fact about past work.

Sentence 3: Use this exact sentence: "Would you know if there's an opening, or who I should reach out to?"{linkedin_line}

Format:
- Start with "Hi {lead_first},"
- Sign off: "{first_name_candidate}" on its own line, nothing else
- Body total under 65 words"""

    else:
        prompt = f"""Ghostwrite a 2-sentence final follow-up email for {candidate_name}, a student.

Recipient: {lead_first} at {lead_company}. This is the last email in a 3-email sequence. No pitch, no skills, no projects.

Write the email body ONLY (greeting + 2 sentences + sign-off). Exactly 2 sentences:

Sentence 1: Say this is your last note. Be brief and direct. Do NOT say "I understand you're busy", "no worries if timing isn't right", "no bandwidth", "I completely understand". Just say something like "Last one from me." short.

Sentence 2: Wish them well at {lead_company} in one warm line. Leave the door open simply. Do NOT use "perhaps", "hopefully", "someday".

Format:
- Start with "Hi {lead_first},"
- Sign off: "{first_name_candidate}" on its own line, no "Best," or "Take care,"
- Body total under 30 words

Good example of tone:
"Last one from me. Wishing you well at {lead_company}, and happy to connect if it's ever useful."

Bad examples (do NOT write like this):
"I completely understand if there's no bandwidth"
"perhaps there's an opportunity to connect"
"hopefully our paths align someday" """

    result = generate_json(
        prompt, schema, temperature=0.85, system_prompt=_EMAIL_SYSTEM_PROMPT,
        deployment=settings.AZURE_OPENAI_EMAIL_DEPLOYMENT,
    )
    body = (result.get("body") or "").strip()
    body = clean_tone(body)

    if not body or len(body) < 20:
        raise ValueError(f"Follow-up body too short for {lead.name}")

    logger.info("[Followup] Generated touch %d for %s (%d words)",
                followup_number + 1, lead.name, len(body.split()))
    return body
