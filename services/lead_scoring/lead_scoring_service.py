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
import re
import unicodedata
from typing import Dict, Any, List

from core.logger import get_logger
from services.candidate_intelligence.payload_builder import usable_dream_companies
from services.shared.ai.apollo_industry_mapper import APOLLO_INDUSTRY_MAP

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════��════════════════════════
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

# Role keywords to filter out — only truly irrelevant titles (not hiring decision makers)
# Recruiters, HR, Talent — these ARE valid outreach targets for job seekers, so not filtered
IRRELEVANT_KEYWORDS = [
    "freelancer", "contractor", "intern",
]
# Whole words only. A substring test dropped "International", "Internal" and
# "Internet" titles (they contain "intern") before they were ever scored.
_IRRELEVANT_TITLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in IRRELEVANT_KEYWORDS) + r")\b"
)


def role_keywords(roles) -> set:
    """The words (over 3 letters) of the student's target roles. A lead whose
    title contains none of them gets the scorer's -25 title penalty."""
    out = set()
    for role in roles or []:
        for word in (role or "").lower().split():
            if len(word) > 3:  # skip short words like "of", "and"
                out.add(word)
    return out


# People who hire for any role at their company. Discovery searches for them
# on purpose (founders and C-level are most of every startup search), but
# their titles rarely contain the student's role words, so the -25 penalty
# lands on them: in 4 days of prod leads (30 Sep), 80% of the 32,886 leads
# with title_relevance -25 were founders or C-level (UC-Q09). "Vice
# president", "product owner" and "founding engineer" are function roles.
_DECISION_MAKER_RE = re.compile(
    r"\b(?:co-?\s?founder|founder|(?<!product )owner|ceo|cto|coo|cpo|cmo|cfo|chief|c\.e\.o|managing director"
    r"|(?<!vice )(?<!vice-)president)\b",
    re.IGNORECASE,
)


def is_decision_maker_title(title) -> bool:
    return bool(_DECISION_MAKER_RE.search(title or ""))


def is_relevant_title(title, keywords) -> bool:
    """A lead worth listing as a match: the title shares a word with the
    student's target roles, or the person hires for the whole company."""
    t = (title or "").lower()
    return any(kw in t for kw in keywords) or is_decision_maker_title(t)


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


# ══════════════════════════════════════════════════════════════════════════════
# INDUSTRY MATCHING (UC-Q04)
# ══════════════════════════════════════════════════════════════════════════════
# Candidates pick industries as quiz slugs ("b2b-saas", "financial_services",
# "healthtech", "edtech"); leads carry free-text industries from the company
# cache ("Software Development", "Financial Services", "Hospitals and Health
# Care"). Exact equality matched almost nothing, so after the 2.7 blend was
# removed industry_relevance was still 5 on 98% of production scores (30 Sep:
# 2,376 of 2,415). Each group: trigger words found in the candidate's term ->
# phrases that count as the same industry in a lead's industry text.
_INDUSTRY_GROUPS: list[tuple[frozenset, tuple[str, ...]]] = [
    (frozenset({"saas", "software", "developer", "devtools", "enterprise", "cloud", "it", "tech",
                "technology", "internet", "consumerapps", "apps", "b2bsaas"}),
     ("software", "saas", "internet", "information technology", "it services", "cloud",
      "computer software", "technology")),
    (frozenset({"fintech", "financial", "finance", "banking", "bank", "bfsi", "asset", "investment",
                "insurance", "insurtech", "wealth", "payments", "lending", "capital", "accounting",
                "vc", "venture", "trading"}),
     ("financial", "fintech", "banking", "bank", "investment", "insurance", "capital markets",
      "asset management", "payments", "lending", "wealth", "accounting", "venture capital")),
    (frozenset({"edtech", "education", "elearning", "learning", "career", "careertech"}),
     ("education", "e learning", "elearning", "edtech", "training", "higher education")),
    (frozenset({"healthtech", "healthcare", "health", "medtech", "biotech", "pharma", "medical",
                "hospital", "wellness", "lifesciences"}),
     ("health", "healthcare", "medical", "hospital", "pharma", "pharmaceutical", "biotech",
      "biotechnology", "wellness", "life sciences", "medtech", "healthtech")),
    (frozenset({"ecommerce", "retail", "marketplace", "d2c", "dtc", "consumer", "fmcg", "cpg"}),
     ("retail", "e commerce", "ecommerce", "consumer", "marketplace", "fmcg", "consumer goods",
      "apparel")),
    (frozenset({"ai", "ml", "artificial", "machine", "genai", "llm", "data"}),
     ("artificial intelligence", "ai", "machine learning", "data")),
    (frozenset({"cybersecurity", "security", "infosec"}),
     ("security", "cybersecurity")),
    (frozenset({"media", "mediatech", "creator", "creatortech", "publishing", "entertainment",
                "gaming", "games", "content"}),
     ("media", "publishing", "entertainment", "broadcast", "games", "gaming", "content",
      "film", "music")),
    (frozenset({"marketing", "advertising", "adtech", "martech", "agency", "agencies"}),
     ("marketing", "advertising", "adtech", "martech")),
    (frozenset({"manufacturing", "robotics", "industrial", "hardware", "automotive", "electronics",
                "semiconductor", "semiconductors", "deeptech", "aerospace"}),
     ("manufacturing", "industrial", "machinery", "robotics", "automation", "automotive",
      "electronics", "hardware", "semiconductor", "semiconductors", "aerospace")),
    (frozenset({"logistics", "supply", "supplychain", "mobility", "transport", "transportation"}),
     ("logistics", "supply chain", "transportation", "shipping", "mobility", "freight")),
    (frozenset({"consulting", "bpo", "outsourcing", "kpo"}),
     ("consulting", "outsourcing", "professional services", "bpo")),
    (frozenset({"nonprofit", "ngo", "philanthropy", "impact"}),
     ("non profit", "nonprofit", "ngo", "philanthropy", "social impact")),
    (frozenset({"agritech", "agriculture", "agri", "farming", "food", "foodtech"}),
     ("agriculture", "agritech", "farming", "food")),
    (frozenset({"govtech", "government", "public", "policy"}),
     ("government", "public sector", "public policy")),
    (frozenset({"travel", "traveltech", "hospitality", "tourism"}),
     ("travel", "hospitality", "tourism", "leisure")),
    (frozenset({"realestate", "proptech", "construction"}),
     ("real estate", "construction", "property")),
    (frozenset({"energy", "climate", "climatetech", "cleantech", "renewable", "renewables",
                "sustainability", "ev"}),
     ("energy", "renewable", "climate", "environmental", "solar", "sustainability")),
    (frozenset({"telecom", "telecommunications"}),
     ("telecom", "telecommunications")),
    (frozenset({"legal", "legaltech", "law"}),
     ("legal", "law")),
    (frozenset({"hr", "hrtech", "recruiting", "recruitment", "staffing", "talent"}),
     ("human resources", "staffing", "recruiting", "recruitment")),
]

# Words that say nothing about the industry on their own.
_INDUSTRY_STOPWORDS = frozenset({
    "and", "or", "of", "the", "b2b", "b2c", "startup", "startups", "native", "company", "companies",
    "industry", "services", "service", "other", "any", "early", "stage", "growth",
})

_GENERIC_TECH_INDUSTRIES = frozenset({
    "computer software", "internet", "information technology and services",
    "information technology", "it services", "it services and it consulting",
    "software development", "software",
})


def _normalize_industry(text: str) -> str:
    """Lower-case, '&', '-', '_' and '/' become spaces, whitespace collapsed."""
    return " ".join(re.sub(r"[-_/&,.()]+", " ", (text or "").lower()).split())


def _industry_tokens(term: str) -> set:
    norm = _normalize_industry(term)
    tokens = set(norm.split()) - _INDUSTRY_STOPWORDS
    if len(tokens) > 1:
        tokens.discard("tech")  # "media-tech" is media, not software; a bare "tech" still is
    compact = norm.replace(" ", "")
    if compact:
        tokens.add(compact)  # "e-commerce" -> "ecommerce", "non_profit" -> "nonprofit"
    return tokens


def _industry_phrases(term: str) -> set:
    """Lead-industry phrases that count as a match for one candidate term."""
    tokens = _industry_tokens(term)
    phrases = set()
    for triggers, group_phrases in _INDUSTRY_GROUPS:
        if tokens & triggers:
            phrases.update(group_phrases)
    # The term's own words also match ("robotics" matches "Robotics").
    phrases.update(t for t in tokens if len(t) >= 4 and t != "tech")
    return phrases


def industry_matches(lead_industry: str, candidate_industries) -> bool:
    """Does the lead's industry text fall in any industry the candidate picked?

    Word-boundary phrase matching, so "ai" never matches "retail". UC-Q04.
    """
    lead_norm = _normalize_industry(lead_industry)
    if not lead_norm or not candidate_industries:
        return False
    if isinstance(candidate_industries, str):
        candidate_industries = [candidate_industries]
    for term in candidate_industries:
        for phrase in _industry_phrases(str(term or "")):
            if re.search(r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", lead_norm):
                return True
    return False


def _is_generic_tech_industry(industry: str) -> bool:
    return _normalize_industry(industry) in _GENERIC_TECH_INDUSTRIES


# ══════════════════════════════════════════════════════════════════════════════
# DREAM COMPANY MATCHING
# ══════════════════════════════════════════════════════════════════════════════
def _name_tokens(name) -> list[str]:
    """Lower-case, accent-free words of a company name. "&" reads as "and"."""
    text = unicodedata.normalize("NFKD", str(name or "")).lower().replace("&", " and ")
    return re.findall(r"[^\W_]+", "".join(ch for ch in text if not unicodedata.combining(ch)))


def _is_joined_run(tokens: list[str], target: str) -> bool:
    """Is target the words of a contiguous run of tokens, written without spaces?"""
    for start in range(len(tokens)):
        run = ""
        for token in tokens[start:]:
            run += token
            if not target.startswith(run):
                break
            if run == target:
                return True
    return False


def company_matches_dream(company, dream_company) -> bool:
    """Does a lead's company name refer to one of the student's dream companies?

    Whole words only, never a raw substring: "ey" is not "Bright Money", "cred"
    is not "Credai Bengaluru", "meta" is not "Metal Avenues". The answer must
    be a contiguous run of whole words in the company name (answer "Google",
    company "Google DeepMind"), or the company name a run of whole words in the
    answer (company "Google", answer "Google India"). The spaces inside the run
    do not count, so "JP Morgan" matches "JPMorgan Chase" and "J.P. Morgan",
    but the run still starts and ends on a word boundary. A name with fewer
    than two letters or digits, including an empty one, never matches.
    """
    company_words, dream_words = _name_tokens(company), _name_tokens(dream_company)
    company_key, dream_key = "".join(company_words), "".join(dream_words)
    if len(company_key) < 2 or len(dream_key) < 2:
        return False
    return _is_joined_run(company_words, dream_key) or _is_joined_run(dream_words, company_key)


_C_LEVEL_RE = re.compile(r"\b(ceo|cto|cfo|coo)\b")
_VP_RE = re.compile(r"\b[sea]?vp\b")  # VP, SVP, EVP, AVP


def _score_seniority_fit(title: str, candidate_seniority: str, company_size: str = "") -> int:
    """Score how well the lead's seniority fits the candidate's level.

    Key fix (May 2026): for seed/tiny companies (1-50 employees), founder/CEO/CTO IS
    the hiring manager regardless of candidate seniority. Penalising c-level contacts
    at 10-person startups was wrong — those are the best possible leads.

    Returns 0-10.
    """
    title_lower = title.lower()

    # Acronyms are matched as whole words. As bare substrings "cto" matched
    # "Director" and "Contractor", and "coo" matched "Coordinator", so a
    # Marketing Coordinator was scored as a COO (1/10 for a student instead of
    # 4). "chief" and "founder" are safe as substrings.
    is_c_level = bool(_C_LEVEL_RE.search(title_lower)) or any(
        kw in title_lower for kw in ["chief", "founder", "co-founder"]
    )
    is_vp = bool(_VP_RE.search(title_lower)) or "vice president" in title_lower
    is_director = "director" in title_lower
    is_head = "head" in title_lower
    is_manager = "manager" in title_lower
    is_lead = any(kw in title_lower for kw in ["lead", "principal", "staff"])
    is_recruiter = any(kw in title_lower for kw in ["recruiter", "talent"])

    # For seed/tiny companies, founders and C-suite ARE the hiring managers.
    # Apply this override before any candidate-seniority logic.
    # Directors are included on purpose: at a 1-50 person company a Director
    # runs a function and hires for it. They used to be caught here only
    # because "director" contains "cto"; keeping them makes that explicit
    # rather than silently re-ranking every seed-stage Director.
    is_seed_company = any(k in (company_size or "") for k in ("1-10", "11-50"))
    if is_seed_company and (is_c_level or is_vp or is_director):
        return 10

    if candidate_seniority in ["entry", "junior", "intern", "student", "graduate", "grad"]:
        # Entry candidates: managers/leads are ideal; VPs/C-level too senior for mid/large cos
        if is_manager:
            return 10
        if is_lead:
            return 9
        if is_recruiter:
            return 7
        if is_head:
            return 6
        if is_director:
            return 4
        if is_vp:
            return 2
        if is_c_level:
            return 1
        return 4

    elif candidate_seniority in ["mid", "career_switching", "switching"]:
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
            return 3
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
    campaign_id: str = "unknown",
    dream_companies: List[str] | None = None,
    company_fit_scores: Dict[str, int] | None = None,
) -> List[Dict[str, Any]]:
    """Score leads using weighted heuristics + LLM company intelligence and return top N.

    Each lead dict is augmented with:
      - score: normalized overall score
      - _title_score, _dept_score, _industry_score, _seniority_score, _location_score:
        raw component scores for DB storage
      - _dream_company_score: bonus points if lead's company matches a dream company
      - _company_fit_score: LLM-evaluated company fit (0-15 pts from 1-10 LLM rating)

    company_fit_scores: dict mapping company_name_lower → 1-10 LLM fit score.
    Leads whose company has no entry are scored on the heuristic alone.
    """
    if not leads:
        return []

    # Filter out completely irrelevant leads
    filtered_leads = []
    for lead in leads:
        title = (lead.get("title") or "").lower()
        if _IRRELEVANT_TITLE_RE.search(title):
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
    role_keywords_ = role_keywords(target_roles)

    # ── Company stage preference for size-mismatch penalty ────────────────
    stage_pref_raw = str(
        (candidate_profile.get("company_preferences", {}).get("company_stage") or ["any"])[0]
        if isinstance(candidate_profile.get("company_preferences", {}).get("company_stage"), list)
        else (candidate_profile.get("company_preferences", {}).get("company_stage") or "any")
    ).lower()
    wants_early_stage = any(kw in stage_pref_raw for kw in ("early", "seed", "under 50"))
    wants_enterprise = any(kw in stage_pref_raw for kw in ("large", "enterprise", "mnc", "2000+"))

    # ── Hard mismatch penalty data (added May 4 2026) ─────────────────────
    # Niches the candidate selected — penalize leads whose company shows zero overlap
    cand_niches = candidate_profile.get("company_preferences", {}).get("niche_keywords", []) or []
    if isinstance(cand_niches, str):
        cand_niches = [cand_niches]
    cand_niches_lower = [n.lower().strip() for n in cand_niches if n and str(n).strip()]

    # Industry denylist for engineering/data/product candidates — these companies
    # exist in Apollo and slip through niche-keyword filters loosely. Confirmed
    # in manual lead review (Beta Makers Lab, WeWork, etc.)
    cand_cluster = (role_intelligence.get("departments") or [""])[0].lower()
    is_tech_candidate = any(c in cand_cluster for c in ("engineer", "data", "product", "design"))
    INDUSTRY_DENYLIST_FOR_TECH = {
        "real estate", "interior design", "construction", "apparel & fashion",
        "hospitality", "food production", "restaurants", "beauty",
        "religious institutions", "luxury goods & jewelry", "wine and spirits",
        "tobacco", "performing arts", "fine art", "cosmetics",
        "ranching", "dairy", "fishery",
    }

    # The stored answers include "No" and "etc"; only names count.
    dream_names = usable_dream_companies(dream_companies)

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

        keyword_matches = [kw for kw in role_keywords_ if kw in title]
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
            # Hard penalty: NO title overlap at all → drag score below the floor.
            # Was +5 (every Operations Manager still scored ~78). Now -25.
            t_score = -25

        # --- 2. Department Relevance (0-20) ---
        d_score = 5
        dept_keywords = list(role_keywords_)
        if any(kw in title for kw in dept_keywords):
            d_score = 20
        elif any(kw in title for kw in ["engineering", "software", "tech", "product", "data", "design"]):
            d_score = 15
        elif any(kw in title for kw in ["operations", "strategy", "business"]):
            d_score = 10

        # --- 3. Industry Match (0-15) ---
        i_score = 5
        if industry and (any(mi == industry for mi in mapped_apollo_industries)
                         or industry_matches(industry, raw_cand_industries)):
            i_score = 15
        elif _is_generic_tech_industry(industry):
            i_score = 12

        # --- 4. Seniority Fit (0-10) ---
        lead_company_size = lead.get("company_size") or ""
        sen_score = _score_seniority_fit(title, candidate_seniority, company_size=lead_company_size)

        # --- 5. Location Relevance (0-10) ---
        l_score = _score_location(location, pref_locations)
        # UC-Q04: Apollo's people search never returns a person's city (0 of
        # 65k recent leads had one), so the line above scored 0 for everyone.
        # A lead found under a person_locations filter is known to be in one of
        # the candidate's cities, which is worth the same as a city match.
        if not location and lead.get("in_preferred_location"):
            l_score = 10

        # --- 6. Dream Company Bonus (0 or +10) ---
        # Dream companies are protected — they get a +10 bonus AND override
        # all penalties (any dream-company match keeps the lead visible no
        # matter how poor the other signals are).
        dc_score = 0
        is_dream = False
        for dc in dream_names:
            if company_matches_dream(lead.get("company"), dc):
                dc_score = 10
                is_dream = True
                break

        # --- 7. Niche-keyword penalty ---
        # Penalize when the candidate selected niches (AI, SaaS, Devtools)
        # but ZERO of them appear anywhere in the company text.
        niche_penalty = 0
        if cand_niches_lower and not is_dream:
            company_blob = " ".join([
                (lead.get("company") or ""),
                (lead.get("industry") or ""),
                (lead.get("company_description") or ""),
            ]).lower()
            niche_hits = sum(1 for n in cand_niches_lower if n in company_blob)
            if niche_hits == 0:
                niche_penalty = -20  # raised from -15: must overcome d_score baseline of +5

        # --- 8. Industry denylist penalty ---
        # Hard mismatch: tech candidate landing on Real Estate / Interior Design / etc.
        # is almost certainly a noise lead — Apollo loosely matched on something.
        denylist_penalty = 0
        if is_tech_candidate and not is_dream and industry:
            if any(d in industry for d in INDUSTRY_DENYLIST_FOR_TECH):
                denylist_penalty = -30  # was -20; strengthened so denylist leads drop below page-1

        # --- 9. Company size mismatch penalty ---
        # Backstop: catches enterprise leads that slip through the probe-loop filter
        # or come from cached/pre-existing lead pools.
        size_penalty = 0
        if not is_dream:
            lead_size = lead_company_size.lower()
            is_large = any(k in lead_size for k in ("1001", "5001", "10000"))
            is_tiny = any(k in lead_size for k in ("1-10", "11-50"))
            if wants_early_stage and is_large:
                size_penalty = -25  # user wants seed-stage, lead is at enterprise
            elif wants_enterprise and is_tiny:
                size_penalty = -10  # user wants large company, lead is at tiny startup

        # --- 10. LLM company intelligence rating (1-10 scale), only when one exists ---
        # UC-Q04: this used to default every unrated company to 2.7 and blend it
        # in at 60%, and discovery never passes ratings, so 60% of every score
        # was the same constant and 98% of scores sat in a 16-57 band. Unrated
        # leads now rank on the heuristic alone; a real rating still blends in.
        llm_rating = None
        if not is_dream:
            lead_company_lower = (lead.get("company") or "").lower().strip()
            llm_rating = (company_fit_scores or {}).get(lead_company_lower)

        # Heuristic score: all dimension scores without LLM contribution
        heuristic_raw = (
            t_score + d_score + i_score + sen_score + l_score
            + dc_score + niche_penalty + denylist_penalty + size_penalty
        )

        # Tie-breaker on heuristic (only when positive — don't randomly rescue penalized leads)
        if heuristic_raw > 0:
            unique_str = str(lead.get("apollo_person_id") or lead.get("name") or "")
            tie_breaker = hash(unique_str) % 5
            heuristic_raw += tie_breaker

        # Normalize heuristic to [0, 100] (raw range [-60, 100])
        heuristic_clamped = max(-60, min(heuristic_raw, 100))
        heuristic_normalized = (heuristic_clamped + 60) * (100 / 160)

        if llm_rating is not None:
            # Normalize LLM rating to [0, 100]; 40% heuristic, 60% company intelligence
            llm_normalized = (llm_rating / 10) * 100
            normalized_score = round(0.4 * heuristic_normalized + 0.6 * llm_normalized, 1)
        else:
            llm_normalized = 0
            normalized_score = round(heuristic_normalized, 1)

        # Dream-company override: floor at 65 so dream matches are always visible
        if is_dream and normalized_score < 65:
            normalized_score = 65.0

        ci_score = round(llm_normalized)  # kept for DB storage compatibility
        lead["score"] = round(normalized_score, 1)
        # Attach raw component scores for DB storage
        lead["_title_score"] = t_score
        lead["_dept_score"] = d_score
        lead["_industry_score"] = i_score
        lead["_seniority_score"] = sen_score
        lead["_location_score"] = l_score
        lead["_dream_company_score"] = dc_score
        lead["_company_fit_score"] = ci_score
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
            "total_raw": heuristic_raw,
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


# ═════════════════════════════════════════════════════════════════════════════
# Domain-affinity post-scoring adjustment (round-2 quality upgrade)
# ═════════════════════════════════════════════════════════════════════════════
# Runs AFTER company_fact_extractor produces structured facts. Compares the
# candidate's actual specialization (resume_profile.subdomain + target_industries)
# against the company's primary_market + core_tech. Hard mismatches get a
# negative adjustment that pushes them below the score floor (hidden from UI).
#
# Mappings: candidate subdomain → set of company primary_market keywords that
# count as "same domain". Verified against the manual lead-review failure cases.
_SUBDOMAIN_TO_TARGET_MARKETS = {
    "machine_learning": {"ai", "ml", "machine learning", "data", "research"},
    "ml_ai": {"ai", "ml", "machine learning", "data", "research"},
    "asr": {"ai", "speech", "voice", "conversation", "ml"},
    "llm": {"ai", "ml", "machine learning", "nlp", "research"},
    "nlp": {"ai", "ml", "machine learning", "nlp", "conversation"},
    "data_science": {"ai", "ml", "data", "analytics"},
    "data_engineering": {"data", "analytics", "platform"},
    "backend": {"backend", "platform", "infrastructure", "developer", "devtools"},
    "frontend": {"frontend", "consumer", "product", "design"},
    "devops": {"devops", "platform", "infrastructure", "developer"},
    "growth_marketing": {"marketing", "growth", "consumer", "b2c"},
    "performance_marketing": {"marketing", "growth", "consumer", "b2c"},
    "brand_marketing": {"marketing", "brand", "consumer"},
    "product_marketing": {"marketing", "product", "saas"},
    "product_design": {"design", "product", "consumer"},
    "ux_research": {"design", "research", "product"},
    "enterprise_sales": {"enterprise", "saas", "b2b"},
}


def domain_affinity_adjustment(
    facts: dict | None,
    subdomain: str | None,
    target_industries: list[str] | None,
    core_tech_overlap: list[str] | None = None,
) -> int:
    """Return a score adjustment in the range [-25, +15] based on whether the
    company's `extracted_facts` match the candidate's actual specialization.

    Returns 0 when there's no facts data (don't penalize what we don't know).
    """
    if not facts:
        return 0

    primary_market = (facts.get("primary_market") or "").lower()
    what_they_build = (facts.get("what_they_build") or "").lower()
    company_blob = f"{primary_market} {what_they_build}"
    core_tech = [str(t).lower() for t in (facts.get("core_tech") or [])]

    score = 0

    # Subdomain affinity — does the company operate in the same problem space?
    if subdomain:
        target_markets = _SUBDOMAIN_TO_TARGET_MARKETS.get(subdomain.lower(), set())
        if target_markets:
            market_hit = any(m in company_blob for m in target_markets)
            tech_hit = any(m in t for m in target_markets for t in core_tech)
            if market_hit or tech_hit:
                score += 10  # bonus: same problem space
            else:
                score -= 25  # hard penalty: different problem space

    # Target-industry overlap (free win when present)
    if target_industries:
        ti = [str(i).lower() for i in target_industries if i]
        if ti and any(i in primary_market for i in ti):
            score += 5

    # Direct core-tech overlap with candidate's actual stack
    if core_tech_overlap:
        cto = [str(t).lower() for t in core_tech_overlap]
        if any(t in core_tech for t in cto):
            score += 5

    # Clamp to declared range
    return max(-25, min(15, score))


def apply_domain_affinity_to_top_leads(
    top_leads: list[dict],
    company_facts_by_domain: dict,
    subdomain: str | None,
    target_industries: list[str] | None,
    candidate_tech_stack: list[str] | None,
) -> dict[int, int]:
    """Compute the adjustment for each top lead. Returns {lead_id: delta}.

    Caller is responsible for adding the delta to LeadScore.overall_score and
    re-saving. We don't mutate here so this stays pure / testable.
    """
    out: dict[int, int] = {}
    for lead in top_leads:
        lid = lead.get("id")
        domain = (lead.get("company_domain") or "").lower()
        facts = company_facts_by_domain.get(domain) if domain else None
        if facts is None:
            name = (lead.get("company") or "").strip()
            if name:
                facts = company_facts_by_domain.get(name) or company_facts_by_domain.get(name.lower())
        delta = domain_affinity_adjustment(
            facts=facts,
            subdomain=subdomain,
            target_industries=target_industries,
            core_tech_overlap=candidate_tech_stack,
        )
        if delta and lid is not None:
            out[lid] = delta
    return out