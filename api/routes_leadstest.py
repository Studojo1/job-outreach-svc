"""Leadstest Routes — Internal experiment page for lead enrichment and resume analysis.

Not linked from any public page. Requires standard auth (session cookie).
No DB writes — all endpoints are read-only for testing purposes.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx
import requests
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from api.dependencies import get_current_user
from core.config import settings
from database.models import User
from services.candidate_intelligence.parser import parse_resume
from services.candidate_intelligence.resume_intelligence import (
    extract_resume_profile,
    extract_enhanced_resume_profile,
)
from services.hiring_signals.career_page_scraper import scrape_career_page

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leadstest", tags=["Leadstest"])

APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"

# Fields we know are in the response but currently ignored
_NEW_PERSON_FIELDS = [
    "headline", "seniority", "departments", "subdepartments", "functions",
    "time_zone", "email_domain_catchall", "photo_url", "github_url",
    "twitter_url", "facebook_url", "extrapolated_email_confidence",
]
_NEW_ORG_FIELDS = [
    "website_url", "primary_domain", "linkedin_url", "founded_year",
    "alexa_ranking", "annual_revenue", "annual_revenue_printed",
    "total_funding", "total_funding_printed",
    "latest_funding_round_date", "latest_funding_stage",
    "organization_headcount_six_month_growth",
    "organization_headcount_twelve_month_growth",
    "organization_headcount_twenty_four_month_growth",
    "technology_names", "keywords", "industries", "secondary_industries",
]


# ── Request / Response Models ────────────────────────────────────────────────

class ApolloInspectRequest(BaseModel):
    first_name: str
    last_name: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    linkedin_url: Optional[str] = None
    apollo_id: Optional[str] = None


class CareerPageRequest(BaseModel):
    company_domain: str
    company_name: Optional[str] = ""


class LinkedInEnrichRequest(BaseModel):
    linkedin_url: str


# ── Tool 1: Resume Analysis Comparator ──────────────────────────────────────

@router.post("/analyze-resume")
async def analyze_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a resume and compare current vs enhanced LLM analysis side by side."""
    contents = await file.read()
    try:
        raw_text, preview = parse_resume(contents, file.filename or "resume.pdf")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Run both LLM analyses in parallel
    loop = asyncio.get_event_loop()
    try:
        current_result, enhanced_result = await asyncio.gather(
            loop.run_in_executor(None, extract_resume_profile, raw_text, preview),
            loop.run_in_executor(None, extract_enhanced_resume_profile, raw_text),
        )
    except Exception as e:
        logger.error("[leadstest] Resume analysis failed: %s", e)
        raise HTTPException(status_code=500, detail=f"LLM analysis failed: {e}")

    current_keys = set(current_result.keys())
    enhanced_keys = set(enhanced_result.keys())
    new_keys = sorted(enhanced_keys - current_keys)

    return {
        "raw_preview": preview,
        "current_analysis": current_result,
        "enhanced_analysis": enhanced_result,
        "delta_summary": {
            "current_field_count": len(current_keys),
            "enhanced_field_count": len(enhanced_keys),
            "new_fields_count": len(new_keys),
            "new_fields": new_keys,
        },
    }


# ── Tool 2: Apollo Data Inspector ────────────────────────────────────────────

@router.post("/apollo-inspect")
async def apollo_inspect(
    req: ApolloInspectRequest,
    current_user: User = Depends(get_current_user),
):
    """Call Apollo People Match and expose the full response, split into
    currently-extracted vs currently-ignored fields."""
    if not settings.APOLLO_API_KEY:
        raise HTTPException(status_code=500, detail="Apollo API key not configured")

    payload: Dict[str, Any] = {}
    if req.apollo_id:
        payload["id"] = req.apollo_id
    if req.first_name:
        payload["first_name"] = req.first_name
    if req.last_name:
        payload["last_name"] = req.last_name
    if req.title:
        payload["title"] = req.title
    if req.company:
        payload["organization_name"] = req.company
    if req.linkedin_url:
        payload["linkedin_url"] = req.linkedin_url

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": settings.APOLLO_API_KEY,
    }

    try:
        resp = requests.post(APOLLO_MATCH_URL, json=payload, headers=headers, timeout=15)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Apollo request failed: {e}")

    if not resp.ok:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Apollo API error: {resp.text[:300]}",
        )

    data = resp.json()
    person = data.get("person") or {}
    org = person.get("organization") or {}

    if not person:
        raise HTTPException(status_code=404, detail="No person found in Apollo for these inputs")

    # ── Currently extracted (what we save to Lead model today) ──
    currently_extracted = {
        "name": person.get("name") or f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
        "email": person.get("email"),
        "email_status": person.get("email_status"),
        "title": person.get("title"),
        "linkedin_url": person.get("linkedin_url"),
        "city": person.get("city"),
        "state": person.get("state"),
        "country": person.get("country"),
        "company": org.get("name"),
        "industry": org.get("industry"),
        "company_size": _employee_count_to_range(org.get("estimated_num_employees")),
        "company_description": (org.get("short_description") or "")[:300] or None,
    }

    # ── Hidden person fields — available but unused ──
    hidden_person = {k: person.get(k) for k in _NEW_PERSON_FIELDS}

    # ── Employment history — clean format ──
    employment_history = []
    for job in (person.get("employment_history") or []):
        employment_history.append({
            "title": job.get("title"),
            "company": job.get("organization_name"),
            "start": _fmt_date(job.get("start_date")),
            "end": _fmt_date(job.get("end_date")),
            "current": job.get("current", False),
        })

    # ── Hidden org fields — available but unused ──
    hidden_company_raw = {k: org.get(k) for k in _NEW_ORG_FIELDS}

    # Format growth rates as percentages for readability
    hidden_company = dict(hidden_company_raw)
    for growth_key in [
        "organization_headcount_six_month_growth",
        "organization_headcount_twelve_month_growth",
        "organization_headcount_twenty_four_month_growth",
    ]:
        val = hidden_company.get(growth_key)
        if isinstance(val, float):
            hidden_company[growth_key] = f"{val * 100:.1f}%"

    # Sample tech stack (full list can be huge)
    tech_names = org.get("technology_names") or []
    hidden_company["technology_names_count"] = len(tech_names)
    hidden_company["technology_names_sample"] = tech_names[:15]
    hidden_company.pop("technology_names", None)

    keywords = org.get("keywords") or []
    hidden_company["keywords_sample"] = keywords[:15]
    hidden_company.pop("keywords", None)

    # ── Derived scoring signals ──
    scoring_signals = _compute_scoring_signals(person, org)

    return {
        "status": "success",
        "currently_extracted": currently_extracted,
        "hidden_person_fields": hidden_person,
        "hidden_person_career": employment_history,
        "hidden_company_fields": hidden_company,
        "scoring_signals_unlocked": scoring_signals,
    }


# ── Tool 3: Career Page Scraper ──────────────────────────────────────────────

@router.post("/career-page")
async def career_page(
    req: CareerPageRequest,
    current_user: User = Depends(get_current_user),
):
    """Scrape a company's career page for open job listings."""
    if not req.company_domain:
        raise HTTPException(status_code=400, detail="company_domain is required")

    result = await scrape_career_page(req.company_domain, req.company_name or "")
    return result


# ── Tool 4: LinkedIn Enrichment via Proxycurl (optional/paid) ────────────────

@router.post("/linkedin-enrich")
async def linkedin_enrich(
    req: LinkedInEnrichRequest,
    current_user: User = Depends(get_current_user),
):
    """Enrich a LinkedIn profile via Proxycurl. Requires PROXYCURL_API_KEY env var."""
    proxycurl_key = getattr(settings, "PROXYCURL_API_KEY", None)
    if not proxycurl_key:
        raise HTTPException(
            status_code=501,
            detail="PROXYCURL_API_KEY not configured. Add it to .env to enable this tool.",
        )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                "https://nubela.co/proxycurl/api/v2/linkedin",
                params={
                    "linkedin_profile_url": req.linkedin_url,
                    "use_cache": "if-present",
                },
                headers={"Authorization": f"Bearer {proxycurl_key}"},
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proxycurl request failed: {e}")

    if not resp.ok:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Proxycurl error: {resp.text[:300]}",
        )

    data = resp.json()

    # Extract most useful fields
    activities = data.get("activities") or []
    recent_post_date = None
    if activities:
        recent_post_date = activities[0].get("time")

    return {
        "status": "success",
        "source": "proxycurl",
        "follower_count": data.get("follower_count"),
        "connection_count": data.get("connections"),
        "headline": data.get("headline"),
        "summary": (data.get("summary") or "")[:300] or None,
        "skills": [s.get("name") for s in (data.get("skills") or [])[:15]],
        "recent_post_date": recent_post_date,
        "activity_count": len(activities),
        "is_active": len(activities) > 0,
        "activity_signal": _activity_signal(len(activities)),
        "raw_field_count": len(data),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _employee_count_to_range(n: Optional[int]) -> Optional[str]:
    if n is None:
        return None
    if n <= 10:
        return "1-10"
    if n <= 50:
        return "11-50"
    if n <= 200:
        return "51-200"
    if n <= 1000:
        return "201-1000"
    if n <= 5000:
        return "1001-5000"
    return "5001+"


def _fmt_date(d: Optional[str]) -> Optional[str]:
    if not d:
        return None
    # Apollo dates look like "2025-01-01T00:00:00.000+00:00" — trim to YYYY-MM
    return d[:7] if len(d) >= 7 else d


def _compute_scoring_signals(person: dict, org: dict) -> dict:
    signals: Dict[str, Any] = {}

    # Hiring urgency from headcount growth
    growth_12m = org.get("organization_headcount_twelve_month_growth")
    growth_6m = org.get("organization_headcount_six_month_growth")
    funding_date = org.get("latest_funding_round_date")

    urgency_score = 0
    urgency_reasons = []

    if isinstance(growth_12m, float):
        pct = growth_12m * 100
        if pct >= 20:
            urgency_score += 40
            urgency_reasons.append(f"{pct:.0f}% headcount growth in 12M")
        elif pct >= 10:
            urgency_score += 25
            urgency_reasons.append(f"{pct:.0f}% headcount growth in 12M")
        elif pct > 0:
            urgency_score += 10
            urgency_reasons.append(f"{pct:.0f}% headcount growth in 12M")
        elif pct < 0:
            urgency_score -= 20
            urgency_reasons.append(f"headcount declined {abs(pct):.0f}% in 12M")

    if isinstance(growth_6m, float) and growth_6m >= 0.05:
        urgency_score += 15
        urgency_reasons.append(f"{growth_6m * 100:.0f}% growth in last 6M")

    if funding_date:
        # Recent funding (within 12 months) is a strong hiring signal
        try:
            from datetime import datetime, timezone
            funded = datetime.fromisoformat(funding_date.replace("Z", "+00:00"))
            months_ago = (datetime.now(timezone.utc) - funded).days / 30
            if months_ago <= 6:
                urgency_score += 25
                urgency_reasons.append(f"funded {months_ago:.0f}M ago ({org.get('latest_funding_stage')})")
            elif months_ago <= 12:
                urgency_score += 15
                urgency_reasons.append(f"funded {months_ago:.0f}M ago ({org.get('latest_funding_stage')})")
        except Exception:
            pass

    signals["hiring_urgency_score"] = min(100, max(0, urgency_score))
    signals["hiring_urgency_reason"] = "; ".join(urgency_reasons) if urgency_reasons else "no growth signals found"

    # Email reliability
    catchall = person.get("email_domain_catchall")
    if catchall is True:
        signals["email_reliability"] = "low"
        signals["email_reliability_reason"] = "catchall domain — email may bounce"
    elif catchall is False:
        signals["email_reliability"] = "high"
        signals["email_reliability_reason"] = "dedicated mailbox"
    else:
        signals["email_reliability"] = "unknown"
        signals["email_reliability_reason"] = "catchall status unknown"

    # Career prestige from employment history
    tier1_companies = {
        "google", "meta", "amazon", "apple", "microsoft", "netflix", "openai",
        "stripe", "airbnb", "uber", "lyft", "twitter", "linkedin", "salesforce",
        "adobe", "oracle", "ibm", "intel", "nvidia", "qualcomm", "goldman sachs",
        "morgan stanley", "mckinsey", "bcg", "bain", "deloitte", "accenture",
        "goldman", "jpmorgan", "jp morgan", "jane street", "two sigma", "citadel",
        "databricks", "snowflake", "palantir", "figma", "notion", "atlassian",
        "shopify", "spotify", "bytedance", "tiktok", "flipkart", "swiggy", "zomato",
        "razorpay", "phonepe", "paytm", "meesho", "ola", "cred", "dream11",
        "infosys", "tcs", "wipro", "hcl", "capgemini",
    }
    emp_history = person.get("employment_history") or []
    prestige_companies = []
    for job in emp_history:
        company_name = (job.get("organization_name") or "").lower()
        if any(t in company_name for t in tier1_companies):
            prestige_companies.append(job.get("organization_name"))

    if prestige_companies:
        signals["career_prestige_tier"] = "tier1"
        signals["career_prestige_reason"] = ", ".join(dict.fromkeys(prestige_companies))
    elif emp_history:
        signals["career_prestige_tier"] = "tier2_or_lower"
        signals["career_prestige_reason"] = "no tier-1 companies detected"
    else:
        signals["career_prestige_tier"] = "unknown"
        signals["career_prestige_reason"] = "no employment history available"

    return signals


def _activity_signal(count: int) -> str:
    if count >= 10:
        return "high"
    if count >= 3:
        return "medium"
    if count >= 1:
        return "low"
    return "inactive"
