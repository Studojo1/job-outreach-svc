"""Lead Scoring Service — weighted heuristic scoring with component breakdown.

Scores leads on 5 dimensions (total 100 points):
  - Title relevance (0-35)
  - Department relevance (0-20)
  - Industry match (0-15)
  - Seniority fit (0-10) — penalizes VP/Director for junior candidates
  - Location relevance (0-10) — city-alias proximity scoring

Returns individual component scores per lead for accurate DB storage.
"""

import json
import os
from typing import Dict, Any, List

from core.logger import get_logger
from services.shared.ai.apollo_industry_mapper import APOLLO_INDUSTRY_MAP

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CITY ALIASES — groups of names that refer to the same city
# ══════════════════════════════════════════════════════════════════════════════
CITY_ALIASES = {
    "bangalore": ["bangalore", "bengaluru", "bengaluru urban", "blr"],
    "mumbai": ["mumbai", "bombay"],
    "delhi": ["delhi", "new delhi", "ncr", "noida", "gurgaon", "gurugram", "faridabad", "ghaziabad"],
    "hyderabad": ["hyderabad", "secunderabad", "cyberabad"],
    "chennai": ["chennai", "madras"],
    "pune": ["pune", "pimpri", "chinchwad"],
    "kolkata": ["kolkata", "calcutta"],
    "ahmedabad": ["ahmedabad", "gandhinagar"],
    "san francisco": ["san francisco", "sf", "bay area", "san jose", "silicon valley", "palo alto", "mountain view", "sunnyvale", "menlo park", "cupertino", "santa clara", "redwood city"],
    "new york": ["new york", "nyc", "manhattan", "brooklyn"],
    "london": ["london"],
    "seattle": ["seattle", "bellevue", "redmond"],
    "austin": ["austin"],
    "boston": ["boston", "cambridge"],
    "toronto": ["toronto"],
    "singapore": ["singapore"],
    "berlin": ["berlin"],
    "amsterdam": ["amsterdam"],
    "los angeles": ["los angeles", "la", "santa monica"],
    "chicago": ["chicago"],
}

# Metro region → state/region mapping
METRO_TO_REGION = {
    "bangalore": "karnataka",
    "mumbai": "maharashtra",
    "delhi": "delhi",
    "hyderabad": "telangana",
    "chennai": "tamil nadu",
    "pune": "maharashtra",
    "kolkata": "west bengal",
    "ahmedabad": "gujarat",
}

# Region → country mapping
REGION_TO_COUNTRY = {
    "karnataka": "india", "maharashtra": "india", "delhi": "india",
    "telangana": "india", "tamil nadu": "india", "west bengal": "india",
    "gujarat": "india", "rajasthan": "india", "kerala": "india",
    "uttar pradesh": "india", "andhra pradesh": "india",
    "california": "united states", "new york": "united states",
    "texas": "united states", "washington": "united states",
    "massachusetts": "united states", "illinois": "united states",
    "ontario": "canada", "british columbia": "canada",
    "england": "united kingdom",
}

# Irrelevant role keywords to filter out
IRRELEVANT_KEYWORDS = [
    "sales", "finance", "recruiter", "consultant",
    "freelancer", "advisor", "hr", "talent"
]


def _resolve_city(text: str) -> str:
    """Resolve a location string to its canonical city name using aliases."""
    text_lower = text.lower().strip()
    for canonical, aliases in CITY_ALIASES.items():
        for alias in aliases:
            if alias in text_lower:
                return canonical
    return ""


def _score_location(lead_location: str, pref_locations: List[str]) -> int:
    """Score location based on city proximity.

    Returns:
        10 — same city (or city alias match)
         9 — same metro sub-area (e.g., Whitefield for Bangalore target)
         7 — same state/region
         4 — same country
         0 — different country or unknown
    """
    if not lead_location or not pref_locations:
        return 0

    lead_lower = lead_location.lower().strip()
    lead_city = _resolve_city(lead_lower)

    for pref in pref_locations:
        pref_lower = pref.lower().strip()
        pref_city = _resolve_city(pref_lower) or pref_lower

        # Same city (exact alias match)
        if lead_city and pref_city and lead_city == pref_city:
            return 10

        # Direct substring match (handles "Bangalore, Karnataka, India" matching "bangalore")
        if pref_lower in lead_lower or lead_lower in pref_lower:
            return 10

        # Same metro sub-area (e.g., "Whitefield" is in Bangalore metro)
        # Check if lead location contains the metro area name
        if pref_city:
            for alias in CITY_ALIASES.get(pref_city, []):
                if alias in lead_lower:
                    return 9

        # Same state/region
        pref_region = METRO_TO_REGION.get(pref_city, "")
        if pref_region and pref_region in lead_lower:
            return 7

        # Same country
        pref_country = REGION_TO_COUNTRY.get(pref_region, "")
        if pref_country and pref_country in lead_lower:
            return 4
        # Fallback country check: "india" in lead location
        if pref_lower == "india" and "india" in lead_lower:
            return 4
        if "india" in pref_lower and "india" in lead_lower:
            return 4

    return 0


def _score_seniority_fit(title: str, candidate_seniority: str) -> int:
    """Score how well the lead's seniority fits the candidate's level.

    For junior candidates: managers/leads score high, VPs/Directors score low.
    For mid-level: managers/directors score high.
    For senior: VPs/Directors/C-level score high.

    Returns 0-10.
    """
    title_lower = title.lower()

    is_c_level = any(kw in title_lower for kw in ["ceo", "cto", "cfo", "coo", "chief", "founder"])
    is_vp = any(kw in title_lower for kw in ["vp", "vice president"])
    is_director = "director" in title_lower
    is_head = "head" in title_lower
    is_manager = "manager" in title_lower
    is_lead = any(kw in title_lower for kw in ["lead", "principal", "staff"])
    is_recruiter = any(kw in title_lower for kw in ["recruiter", "talent"])

    if candidate_seniority in ["entry", "junior", "intern", "student", "graduate", "grad"]:
        # Junior candidates: managers and leads are ideal, VPs are too senior
        if is_manager:
            return 10
        if is_lead:
            return 9
        if is_recruiter:
            return 7
        if is_head:
            return 5
        if is_director:
            return 3
        if is_vp:
            return 1
        if is_c_level:
            return 0
        return 4  # Unknown seniority

    elif candidate_seniority in ["mid", "career_switching", "switching"]:
        # Mid candidates: directors and managers are ideal
        if is_director:
            return 10
        if is_manager:
            return 9
        if is_head:
            return 8
        if is_lead:
            return 7
        if is_vp:
            return 5
        if is_recruiter:
            return 6
        if is_c_level:
            return 2
        return 4

    else:
        # Senior/experienced candidates: VPs, directors, C-level ideal
        if is_vp:
            return 10
        if is_director:
            return 9
        if is_c_level:
            return 8
        if is_head:
            return 8
        if is_manager:
            return 5
        if is_lead:
            return 4
        return 3


def score_and_select_leads(
    leads: List[Dict[str, Any]],
    candidate_profile: Dict[str, Any],
    role_intelligence: Dict[str, Any],
    target_count: int = 200,
    campaign_id: str = "unknown"
) -> List[Dict[str, Any]]:
    """Score leads using weighted heuristics and return top N.

    Each lead dict is augmented with:
      - score: normalized overall score (70-90 range)
      - _title_score, _dept_score, _industry_score, _seniority_score, _location_score:
        raw component scores for DB storage
    """
    if not leads:
        return []

    # Filter out completely irrelevant leads
    filtered_leads = []
    for lead in leads:
        title = (lead.get("title") or "").lower()
        if any(bad_kw in title for bad_kw in IRRELEVANT_KEYWORDS):
            continue
        filtered_leads.append(lead)

    # Extract candidate context
    raw_cand_industries = candidate_profile.get("company_preferences", {}).get("industries", [])
    if isinstance(raw_cand_industries, str):
        raw_cand_industries = [raw_cand_industries]

    cand_industries_lower = [i.lower() for i in raw_cand_industries]
    mapped_apollo_industries = []
    for cand_ind in cand_industries_lower:
        found = False
        for apollo_cat, terms in APOLLO_INDUSTRY_MAP.items():
            if cand_ind in [t.lower() for t in terms]:
                mapped_apollo_industries.append(apollo_cat.lower())
                found = True
        if not found:
            mapped_apollo_industries.append(cand_ind)

    # Location preferences
    pref_locations = candidate_profile.get("location_preferences", [])

    # Candidate seniority for authority scoring
    candidate_seniority = role_intelligence.get("candidate_seniority", "entry")

    # Build dynamic keyword cluster from candidate's preferred roles
    target_roles = [r.lower() for r in
                    candidate_profile.get("preferred_roles", []) +
                    candidate_profile.get("target_roles", [])]

    # Extract keywords from target roles for cluster matching
    role_keywords = set()
    for role in target_roles:
        for word in role.split():
            if len(word) > 3:  # skip short words like "of", "and"
                role_keywords.add(word)

    scored_leads = []
    log_traces = []

    for lead in filtered_leads:
        title = (lead.get("title") or "").lower()
        industry = (lead.get("industry") or "").lower()
        location = lead.get("location") or ""

        # --- 1. Title Relevance (0-35) ---
        t_score = 0
        exact_role_match = any(tr == title for tr in target_roles)
        partial_role_match = any(tr in title or title in tr for tr in target_roles if tr)

        keyword_matches = [kw for kw in role_keywords if kw in title]
        match_count = len(keyword_matches)

        if exact_role_match:
            t_score = 35
        elif partial_role_match and "manager" in title:
            t_score = 33
        elif partial_role_match:
            t_score = 30
        elif match_count >= 2 and "manager" in title:
            t_score = 28
        elif match_count >= 2:
            t_score = 24
        elif match_count == 1 and "manager" in title:
            t_score = 20
        elif match_count == 1:
            t_score = 15
        else:
            t_score = 5

        # --- 2. Department Relevance (0-20) ---
        d_score = 5
        dept_keywords = list(role_keywords)
        if any(kw in title for kw in dept_keywords):
            d_score = 20
        elif any(kw in title for kw in ["engineering", "software", "tech", "product", "data", "design"]):
            d_score = 15
        elif any(kw in title for kw in ["operations", "strategy", "business"]):
            d_score = 10

        # --- 3. Industry Match (0-15) ---
        i_score = 5
        if any(mi == industry for mi in mapped_apollo_industries) and industry:
            i_score = 15
        elif industry in ["computer software", "internet", "information technology and services"]:
            i_score = 12

        # --- 4. Seniority Fit (0-10) ---
        sen_score = _score_seniority_fit(title, candidate_seniority)

        # --- 5. Location Relevance (0-10) ---
        l_score = _score_location(location, pref_locations)

        # Total Calculation
        total_score = t_score + d_score + i_score + sen_score + l_score

        # Tie-breaker
        unique_str = str(lead.get("apollo_person_id") or lead.get("name") or "")
        tie_breaker = hash(unique_str) % 5
        total_score += tie_breaker
        total_score = min(total_score, 100)

        # Normalize to 70-90 range
        normalized_score = 70 + (total_score / 100) * 20

        lead["score"] = round(normalized_score, 1)
        # Attach raw component scores for DB storage
        lead["_title_score"] = t_score
        lead["_dept_score"] = d_score
        lead["_industry_score"] = i_score
        lead["_seniority_score"] = sen_score
        lead["_location_score"] = l_score
        scored_leads.append(lead)

        log_traces.append({
            "lead_id": lead.get("id") or lead.get("linkedin_url"),
            "name": lead.get("name"),
            "title": lead.get("title"),
            "location": location,
            "title_score": t_score,
            "department_score": d_score,
            "industry_score": i_score,
            "seniority_score": sen_score,
            "location_score": l_score,
            "total_raw": total_score,
            "score_normalized": lead["score"],
        })

    # Sort descending and take top N
    scored_leads.sort(key=lambda x: x.get("score", 0), reverse=True)
    result = scored_leads[:target_count]

    # Log location scoring summary
    loc_scores = [t["location_score"] for t in log_traces]
    if loc_scores:
        avg_loc = sum(loc_scores) / len(loc_scores)
        perfect = sum(1 for s in loc_scores if s == 10)
        zero = sum(1 for s in loc_scores if s == 0)
        logger.info("[LeadScoring] Location summary: avg=%.1f, perfect=%d, zero=%d (target=%s)",
                    avg_loc, perfect, zero, pref_locations)

    # Log seniority scoring summary
    sen_scores = [t["seniority_score"] for t in log_traces]
    if sen_scores:
        avg_sen = sum(sen_scores) / len(sen_scores)
        logger.info("[LeadScoring] Seniority summary: avg=%.1f, candidate_level=%s", avg_sen, candidate_seniority)

    # Dump trace
    try:
        os.makedirs("logs", exist_ok=True)
        trace_file = f"logs/lead_scoring_trace_{campaign_id}.json"
        with open(trace_file, "w") as f:
            json.dump(log_traces[:50], f, indent=2)  # Sample first 50
        logger.info("[SCORING] Written trace for %d leads to %s", len(log_traces), trace_file)
    except Exception as e:
        logger.error("[SCORING] Failed to write scoring trace: %s", e)

    return result