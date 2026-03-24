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
  "domain": "<primary field, e.g. software_engineering | marketing | finance | data | product | design | sales | operations | hr | legal | other>",
  "subdomain": "<specific niche, e.g. backend | growth_marketing | investment_banking | data_science>",
  "seniority": "<student | junior | mid | senior>",
  "experience_years": <number or null>,
  "top_skills": ["<skill1>", "<skill2>", "<skill3>"],
  "geography": {
    "city": "<current city or null>",
    "country": "<country name or null>",
    "country_code": "<ISO 3166-1 alpha-2 or null>"
  },
  "likely_roles": ["<role title 1>", "<role title 2>", "<role title 3>"],
  "education_level": "<high_school | bachelors | masters | phd | other | unknown>",
  "target_industries": ["<industry1>", "<industry2>"]
}

Rules:
- likely_roles: 3-5 specific, realistic role titles suited to this candidate (e.g. "Backend Engineer", "Growth Marketing Manager")
- top_skills: top 5 most marketable skills from the resume
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
    from core.config import get_settings
    from openai import AzureOpenAI

    settings = get_settings()
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
