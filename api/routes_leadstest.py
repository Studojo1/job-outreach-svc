"""Leadstest Routes — Internal experiment page for lead enrichment and resume analysis.

Not linked from any public page. Requires standard auth (session cookie).
No DB writes — all endpoints are read-only for testing purposes.
"""

import asyncio
import logging
from typing import Optional

import httpx
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
from services.hiring_signals.ddg_search import (
    ddg_search,
    extract_linkedin_profile,
    extract_linkedin_company,
    extract_company_domain,
)
from services.hiring_signals.news_search import get_company_news

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leadstest", tags=["Leadstest"])


# ── Request / Response Models ────────────────────────────────────────────────

class LeadIntelRequest(BaseModel):
    first_name: str
    title: str
    company_name: str


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


# ── Tool 2: Lead Intelligence (DuckDuckGo + Google News RSS) ─────────────────

@router.post("/lead-intel")
async def lead_intel(
    req: LeadIntelRequest,
    current_user: User = Depends(get_current_user),
):
    """Given first name, last name, company — gather all public intelligence for free.

    Runs 4 DuckDuckGo searches + Google News RSS in parallel, then scrapes
    the career page if a domain is found. No API keys, no credits consumed.
    """
    company = req.company_name

    logger.info("[LeadIntel] Gathering intel for %s (%s) @ %s", req.first_name, req.title, company)

    person_li_results, domain_results, company_li_results, news = await asyncio.gather(
        ddg_search(f'"{req.first_name}" "{req.title}" "{company}" site:linkedin.com/in', max_results=5),
        ddg_search(f'"{company}" official website', max_results=5),
        ddg_search(f'"{company}" linkedin company', max_results=5),
        get_company_news(company, max_results=8),
    )

    person_linkedin_url = extract_linkedin_profile(person_li_results)
    company_domain = extract_company_domain(domain_results)
    company_linkedin_url = extract_linkedin_company(company_li_results)

    careers = None
    if company_domain:
        try:
            careers = await scrape_career_page(company_domain, company)
        except Exception as exc:
            logger.warning("[LeadIntel] Career page scrape failed for %s: %s", company_domain, exc)

    logger.info(
        "[LeadIntel] Done for %s @ %s — linkedin=%s domain=%s news=%d careers_status=%s",
        req.first_name, company, person_linkedin_url, company_domain, len(news),
        careers.get("status") if careers else "skipped",
    )

    return {
        "person_linkedin_url": person_linkedin_url,
        "person_linkedin_search_results": person_li_results[:3],
        "company_domain": company_domain,
        "company_linkedin_url": company_linkedin_url,
        "funding_news": news,
        "careers": careers,
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


def _activity_signal(count: int) -> str:
    if count >= 10:
        return "high"
    if count >= 3:
        return "medium"
    if count >= 1:
        return "low"
    return "inactive"
