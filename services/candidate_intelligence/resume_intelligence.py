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
  "likely_roles": ["<role title 1>", "<role title 2>", "<role title 3>", "<role title 4>"],
  "education_level": "<high_school | bachelors | masters | phd | other | unknown>",
  "target_industries": ["<industry1>", "<industry2>"]
}

Use these career clusters to pick likely_roles — choose specific role titles that match the candidate's actual skills:
- Technology & Engineering: Software Engineer, Backend Developer, Frontend Developer, Full-Stack Developer, Mobile Developer, DevOps Engineer, Cloud Engineer, Security Analyst, QA Engineer
- Data & Analytics: Data Analyst, Data Scientist, ML Engineer, Analytics Engineer, BI Analyst, Product Analyst
- Marketing & Growth: Growth Marketing Manager, Performance Marketer, Content Marketing Specialist, Product Marketing Manager, SEO Specialist, Marketing Analyst
- Sales & Business Development: SDR, Account Executive, Business Development Representative, Enterprise Sales Associate, Partnerships Manager
- Product: Product Manager, Associate PM, Product Analyst, Go-to-Market Analyst
- Design & Creative: UX Designer, Product Designer, UI Designer, Visual Designer, UX Researcher
- Finance & Accounting: Financial Analyst, IB Analyst, FP&A Analyst, Equity Research Analyst, VC Analyst
- Operations & Supply Chain: Operations Analyst, Project Manager, Supply Chain Analyst, Business Operations Associate
- Consulting & Strategy: Management Consultant (Analyst), Strategy Analyst, Associate Consultant

Rules:
- likely_roles: 3-5 realistic roles for THIS candidate based on their actual experience and skills
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

    client = AzureOpenAI(
        api_key=settings.AZURE_OPENAI_API_KEY,
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
        model=settings.AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Resume:\n{trimmed}"},
        ],
        temperature=0,
        max_tokens=400,
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
# Background task wrapper — safe, logs errors, never raises
# ---------------------------------------------------------------------------

def extract_and_store_resume_profile(candidate_id: int, db_session_factory: Any) -> None:
    """
    Intended to be called as a FastAPI BackgroundTask.
    Opens its own DB session (background tasks run after response is sent,
    so the request session is already closed).
    """
    try:
        db = db_session_factory()
        try:
            from database.models import Candidate
            candidate = db.query(Candidate).filter_by(id=candidate_id).first()
            if not candidate:
                logger.warning(f"[ResumeIntelligence] Candidate {candidate_id} not found")
                return
            if candidate.resume_profile:
                logger.info(f"[ResumeIntelligence] Candidate {candidate_id} already has profile, skipping")
                return

            logger.info(f"[ResumeIntelligence] Extracting profile for candidate {candidate_id}")
            profile = extract_resume_profile(
                resume_text=candidate.resume_text or "",
                parsed_json=candidate.parsed_json,
            )
            candidate.resume_profile = profile
            db.commit()
            logger.info(f"[ResumeIntelligence] Profile stored for candidate {candidate_id}: domain={profile.get('domain')}, seniority={profile.get('seniority')}")
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"[ResumeIntelligence] Background extraction failed for candidate {candidate_id}: {exc}", exc_info=True)
