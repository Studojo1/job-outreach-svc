"""
Resume intelligence — single LLM call after upload to extract structured profile.
Runs as a FastAPI BackgroundTask; result stored in candidates.resume_profile JSONB.
Used by question_engine.py from Q3 onward to personalise question options.
"""

from __future__ import annotations
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a career intelligence extractor. Given a resume, extract a compact structured profile.
Output ONLY valid JSON — no markdown, no prose, no code fences.

Required JSON schema:
{
  "domain": "<primary field: software_engineering | marketing | finance | data | product | design | sales | operations | hr | legal | consulting | healthcare | media | education | manufacturing | other>",
  "subdomain": "<specific niche, e.g. backend | frontend | growth_marketing | investment_banking | data_science | product_management | ux_design>",
  "seniority": "<student | junior | mid | senior>",
  "experience_years": <number or null>,
  "top_skills": ["<skill1>", "<skill2>", "<skill3>", "<skill4>", "<skill5>"],
  "geography": {
    "city": "<candidate's CURRENT city from contact/header section only — NOT company locations>",
    "country": "<candidate's CURRENT country — NOT countries of companies they worked at>",
    "country_code": "<ISO 3166-1 alpha-2 of candidate's current location, or null>"
  },
  "likely_roles": ["<role 1>", "<role 2>", "<role 3>", "<role 4>", "<role 5>", "<role 6>"],
  "education_level": "<high_school | bachelors | masters | phd | other | unknown>",
  "target_industries": ["<industry1>", "<industry2>"]
}

Use these career clusters to pick likely_roles — choose specific role titles that match the candidate's actual skills and career pattern:
- Technology & Engineering: Software Engineer, Backend Developer, Frontend Developer, Full-Stack Developer, Mobile Developer, DevOps Engineer, Cloud Engineer, Security Analyst, QA Engineer
- AI & Applied AI: AI Engineer, Applied AI Engineer, LLM Engineer, GenAI Engineer, AI Agent Engineer, Speech / ASR Engineer, NLP Engineer, Computer Vision Engineer, Machine Learning Engineer, Research Scientist, AI Automation Engineer, AI Solutions Architect
- Data & Analytics: Data Analyst, Data Scientist, Analytics Engineer, BI Analyst, Product Analyst
- Marketing & Growth: Growth Marketing Manager, Performance Marketer, Content Marketing Specialist, Product Marketing Manager, SEO Specialist, GTM Systems Builder, Growth Hacker
- Sales & Business Development: SDR, Account Executive, Business Development Representative, Enterprise Sales Associate, Partnerships Manager, AI Sales Engineer
- Product: Product Manager, AI Product Manager, Associate PM, Founding PM, Product Analyst
- Design & Creative: UX Designer, Product Designer, UI Designer, Visual Designer, UX Researcher
- Finance & Accounting: Financial Analyst, IB Analyst, FP&A Analyst, Equity Research Analyst, VC Analyst
- Operations & Founder's Office: Operations Analyst, Business Operations Associate, Founder's Office, Chief of Staff, Project Manager, Supply Chain Analyst
- Strategy & Consulting: Management Consultant (Analyst), Strategy Analyst, Associate Consultant
- Cross-functional / Compound (use when resume shows 2+ distinct skill areas working together): Founding Product Engineer, AI Product Strategist, Founder's Office (AI + Growth), GTM Automation Engineer, AI Automation Consultant, Product-Led Growth Operator, Zero-to-One Builder, Applied AI Generalist

Rules:
- likely_roles: 4-6 realistic roles for THIS candidate based on their actual experience and skills.
- CRITICAL: If the resume shows cross-functional signals — e.g. someone who builds AI systems AND owns growth/GTM/distribution, OR an engineer who ships products AND talks to clients/users — ALWAYS include compound titles from the Cross-functional cluster. These describe who the person actually is better than single-function titles.
- Pick the MOST SPECIFIC titles — e.g. for an ASR engineer prefer "Speech / ASR Engineer" over "ML Engineer". For an AI builder who also owns GTM, prefer "Founding Product Engineer" or "AI Product Strategist" over just "AI Engineer".
- Prioritise titles the person could actually apply for and get, given their demonstrated experience level.
- geography: only use the candidate's own location (from phone number, address, header) — ignore company office locations
- top_skills: the 5 most marketable skills demonstrated in the resume
- seniority: infer from years of experience and education stage
- Use null for fields you cannot determine
- Respond with JSON only"""


def extract_resume_profile(
    resume_text: str,
    parsed_json: dict | None = None,
) -> dict:
    """
    Run a single LLM call to extract structured resume intelligence.
    Returns the profile dict (to be stored in candidates.resume_profile).
    Raises on LLM failure — caller should catch and log.
    """
    from core.config import settings
    from openai import AzureOpenAI

    # NOTE: settings field names are AZURE_OPENAI_KEY (not _API_KEY) and
    # AZURE_OPENAI_LLM_DEPLOYMENT (not _DEPLOYMENT). The previous mismatch
    # caused this whole function to silently fail with AttributeError for
    # every uploaded resume.
    client = AzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )

    # Trim resume to avoid token waste — first 3000 chars captures the key info
    trimmed = (resume_text or "")[:3000]
    if parsed_json:
        # Prepend structured data hint for better extraction
        hint = f"Structured parse preview: {json.dumps(parsed_json, default=str)[:500]}\n\n"
        trimmed = hint + trimmed

    response = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Resume:\n{trimmed}"},
        ],
        temperature=0,
        max_completion_tokens=600,
    )

    raw = response.choices[0].message.content or ""
    return _parse_json(raw)


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Extract bare JSON object
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw[start:end + 1])
        raise ValueError(f"Could not parse resume profile JSON: {raw[:200]}")


# ---------------------------------------------------------------------------
# Enhanced extraction v2 — deep career intelligence for leadstest
# ---------------------------------------------------------------------------

_ENHANCED_SYSTEM_PROMPT = """You are a senior talent strategist and career intelligence analyst. Your job is to read a resume and produce a structured intelligence brief that a sharp recruiter or founder would write after a 30-minute coffee chat — not a keyword dump, but genuine insight about who this person is, what makes them tick, and where they would thrive.

Output ONLY valid JSON — no markdown, no prose, no code fences.

Required JSON schema:
{
  "domain": "<primary field: software_engineering | marketing | finance | data | product | design | sales | operations | hr | legal | consulting | healthcare | media | education | manufacturing | other>",
  "subdomain": "<specific niche: e.g. backend | frontend | growth_marketing | investment_banking | data_science | product_management | ux_design | ai_engineering | gtm_strategy | revenue_operations>",
  "seniority": "<student | junior | mid | senior>",
  "experience_years": <number or null>,
  "top_skills": ["<skill1>", "<skill2>", "<skill3>", "<skill4>", "<skill5>"],
  "geography": {
    "city": "<candidate's CURRENT city from contact/header section only — NOT company locations>",
    "country": "<candidate's CURRENT country>",
    "country_code": "<ISO 3166-1 alpha-2 or null>"
  },
  "likely_roles": ["<role title 1>", "<role title 2>", "<role title 3>", "<role title 4>", "<role title 5>"],
  "education_level": "<high_school | bachelors | masters | phd | other | unknown>",
  "target_industries": ["<industry1>", "<industry2>"],

  "achievement_density": <integer 0-100: % of resume bullets that contain a real number, metric, scale signal, or measurable outcome — "reduced latency by 40%", "led team of 5", "10M requests/day" all count; "responsible for X" or "helped with Y" do NOT count>,
  "trajectory_speed": "<fast | normal | slow>: fast = promotions, rapid scope growth, or title jumps within 1-2 years; slow = same title 4+ years with no evident scope change",
  "company_prestige_tier": "<tier1 | tier2 | tier3 | unknown>: tier1 = FAANG, McKinsey, Goldman Sachs, Stripe, Airbnb, Uber, top-20 global brands; tier2 = well-known regional/mid-size companies with 200+ employees; tier3 = small, early-stage, or obscure companies",
  "has_portfolio_signal": <true | false: any GitHub URL, personal website, portfolio link, Behance, Dribbble, or project URL visible in the resume>,
  "most_recent_stack": ["<tool or skill from their last/current role — max 5>"],
  "role_progression": "<ic_only | ic_to_manager | multi_company_growth | same_company_growth>",

  "archetype_label": "<the single most accurate compound role archetype for this person — NOT a job title from a job board, but a label that describes the intersection of their skills, working style, and career pattern. Examples: 'Founding Product Engineer', 'AI-Native Founder's Office Hire', 'Zero-to-One Growth Systems Builder', 'Applied AI Solutions Architect', 'GTM Automation Engineer', 'Full-Stack AI Builder', 'Product-Led Growth Operator', 'Consulting-to-Startup Generalist'. Never output a generic single-function title like 'Software Engineer' or 'Product Manager' unless the resume truly shows no compound pattern.>",
  "archetype_reason": "<2-3 sentences explaining exactly WHY this archetype fits — must reference specific companies, roles, projects, or skill combinations from the actual resume. Bad: 'Has diverse skills and experience.' Good: 'Built and shipped production AI voice systems at FourFrontAI for real estate and healthcare clients while simultaneously owning growth and scaling Studojo to 2,500+ users — that rare combination of zero-to-one technical delivery AND commercial distribution instinct is the exact DNA that early-stage AI startups need in a founding-team-adjacent hire.'>",
  "company_stage_fit": ["<array of best-fit company stages based on resume evidence: pre-seed | seed | series-a | series-b | growth | enterprise — include all that genuinely fit, not just one>"],
  "company_type_best_fit": ["<array of company types where this profile would thrive: ai-native-startup | automation-agency | vertical-saas | b2b-saas | marketplace | deep-tech | creator-tech | gtm-tools | consulting-firm | fintech | enterprise-software | media-tech | edtech | healthtech>"],
  "company_type_avoid": ["<array of company/environment types that would be a POOR fit — be honest and specific. Examples: big-tech-enterprise | deep-research-lab | slow-moving-mnc | hyper-process-consulting | large-structured-org | pure-execution-role. These anti-fit signals are as valuable as the positive ones.>"],
  "execution_style": "<builder-first | strategist-first | operator-first | hybrid>: builder-first = ships things, makes things work; strategist-first = frames problems, designs systems; operator-first = runs and scales existing things; hybrid = genuinely balances 2+ of these",
  "ambiguity_tolerance": "<high | medium | low>: high = thrives with minimal direction, figures it out; low = needs clear specs and structure to do their best work",
  "ownership_orientation": "<end-to-end | scoped | collaborative>: end-to-end = owns outcomes from concept to delivery; scoped = executes well within defined boundaries; collaborative = strong in team-dependent environments",
  "breadth_vs_depth": "<generalist | specialist | T-shaped>: generalist = wide range of domains with no single deep spike; specialist = deep single-domain expertise; T-shaped = one clear deep domain with meaningful adjacent breadth",
  "distribution_signal": <true | false: true only if the resume shows EVIDENCE of growth hacking, content creation, community building, client acquisition, user acquisition, sales, GTM, or business development IN ADDITION TO technical or analytical skills. This 'builds AND distributes' pattern is rare — flag it when genuinely present.>,
  "vertical_exposure": ["<industries where this person has actual delivery experience from the resume — NOT aspirational interests. Be specific: real-estate-tech | healthcare-ai | career-tech | voice-ai | recruiting-tech | fintech | edtech | e-commerce | b2b-saas | retail | logistics | gaming | media | climate-tech>"],
  "strongest_hook": "<exactly 1 sentence starting with a strong verb or metric: the single most impressive, specific, concrete thing about this candidate. Prefer quantified outcomes. Bad: 'Experienced in AI and product.' Good: 'Co-built a career-tech platform to 2,500+ signups and 200+ student placements within months using AI-powered outreach tools — while concurrently delivering production AI systems for Microsoft-adjacent clients.'",
  "candidate_pitch": "<exactly 2 sentences from the recruiter's perspective: (1) what makes this profile genuinely compelling and rare, (2) what specific type of team or company would be most excited to have them. Write for a founder or hiring manager, not HR.>"
}

Field-by-field rules:
- archetype_label: compound archetypes only. Think: what IS this person, not what do they apply for. "Founding Product Engineer" is better than "Product Manager + Software Engineer". If the profile is truly single-function, name it precisely but don't force a compound label.
- archetype_reason: cite the resume. Name actual companies, quantified outcomes, or specific skill combinations. Never write generic sentences.
- company_stage_fit: infer from evidence — startup experience + fast trajectory + broad ownership = ["seed", "series-a"]; FAANG/tier1 + deep specialization = ["series-b", "growth", "enterprise"]. Be honest — NOT everyone fits early-stage.
- company_type_avoid: this is critical. A builder-first generalist should avoid "deep-research-lab". A high-ambiguity-tolerance person should avoid "large-structured-org". Be specific. Don't leave this empty.
- distribution_signal: only true if there is real evidence in the resume — not just because the person works in tech or did one user interview. Actual growth numbers, client acquisition, community building, or content distribution count.
- vertical_exposure: list only industries where the resume shows actual work done — not industries the candidate is "interested in". If they built an AI product for real estate, include "real-estate-tech". If they just listed "interested in fintech" with no experience, exclude it.
- strongest_hook: must be a sentence a recruiter would actually use to pitch this candidate. Concrete, specific, quotable.
- candidate_pitch: write as if briefing a founder. Sentence 1: what's genuinely rare or impressive here. Sentence 2: who specifically would get excited about this profile.
- likely_roles: 4-6 specific realistic titles from the clusters below, chosen for THIS candidate's actual background. Prefer compound or specialised titles (e.g. "AI Agent Engineer" not "AI Engineer", "GTM Systems Builder" not "Marketing Manager").
- Use null for fields you cannot determine from the resume.
- Respond with JSON only.

Role cluster reference (use when choosing likely_roles):
Technology: Software Engineer, Backend Developer, Frontend Developer, Full-Stack Developer, Mobile Developer, DevOps Engineer, Cloud Engineer
AI/ML: ML Engineer, AI Engineer, Applied AI Engineer, LLM Engineer, GenAI Engineer, AI Agent Engineer, Speech/ASR Engineer, NLP Engineer, Computer Vision Engineer, Research Scientist
Data: Data Analyst, Data Scientist, Analytics Engineer, BI Analyst, Product Analyst, AI/ML Ops Engineer
GTM/Growth: Growth Marketing Manager, Performance Marketer, GTM Systems Builder, Product Marketing Manager, SEO Specialist, Growth Hacker
Sales: SDR, Account Executive, Business Development Rep, Enterprise Sales Associate, Partnerships Manager
Product: Product Manager, Associate PM, Product Analyst, AI Product Manager, Founding PM
Design: UX Designer, Product Designer, UI Designer, UX Researcher
Finance: Financial Analyst, IB Analyst, FP&A Analyst, Equity Research Analyst, VC Analyst
Operations: Operations Analyst, Project Manager, Supply Chain Analyst, Business Operations Associate, Founder's Office
Consulting: Management Consultant (Analyst), Strategy Analyst, Associate Consultant"""


def extract_enhanced_resume_profile(resume_text: str) -> dict:
    """Run enhanced LLM v2 extraction — 20+ intelligence fields including archetype, company fit, work style DNA.

    Intended for the leadstest comparison tool and production quiz personalization.
    Not stored to DB from here — returned directly to the caller.
    """
    from core.config import settings
    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )

    trimmed = (resume_text or "")[:5000]  # more context for richer intelligence extraction

    response = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _ENHANCED_SYSTEM_PROMPT},
            {"role": "user", "content": f"Resume:\n{trimmed}"},
        ],
        temperature=0,
        max_completion_tokens=1200,
    )

    raw = response.choices[0].message.content or ""
    return _parse_json(raw)


# ---------------------------------------------------------------------------
# Background task wrapper — safe, logs errors, never raises
# ---------------------------------------------------------------------------

def extract_and_store_resume_profile(candidate_id: int, db_session_factory: Any) -> None:
    """FastAPI BackgroundTask wrapper. Uses the v2 enhanced extraction so that
    archetype_label, company_stage_fit, company_type_avoid, execution_style,
    distribution_signal, etc. are stored to candidates.resume_profile and
    available to the quiz engine for personalised suggestions.

    Re-extracts if the stored profile is v1 (missing archetype_label).
    """
    try:
        db = db_session_factory()
        try:
            from database.models import Candidate
            candidate = db.query(Candidate).filter_by(id=candidate_id).first()
            if not candidate:
                logger.warning(f"[ResumeIntelligence] Candidate {candidate_id} not found")
                return

            existing = candidate.resume_profile
            if isinstance(existing, dict) and existing.get("archetype_label"):
                logger.info(f"[ResumeIntelligence] Candidate {candidate_id} already has v2 profile, skipping")
                return

            logger.info(f"[ResumeIntelligence] Extracting v2 profile for candidate {candidate_id}")
            profile = extract_enhanced_resume_profile(
                resume_text=candidate.resume_text or "",
            )
            candidate.resume_profile = profile
            db.commit()
            logger.info(
                f"[ResumeIntelligence] v2 profile stored for candidate {candidate_id}: "
                f"domain={profile.get('domain')}, archetype={profile.get('archetype_label')}, "
                f"stage_fit={profile.get('company_stage_fit')}"
            )
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"[ResumeIntelligence] Background extraction failed for candidate {candidate_id}: {exc}", exc_info=True)
