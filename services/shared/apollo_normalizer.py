"""Apollo field normalization — maps user-facing values to Apollo API values.

Apollo uses specific industry names and location formats.
User-provided values must be normalized to match.
"""

from typing import List, Optional

from core.logger import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# INDUSTRY MAPPING — user/LLM values → Apollo API industry names
# ═══════════════════════════════════════════════════════════════════════════════

_INDUSTRY_MAP = {
    # Exact Apollo industry names (pass through)
    "computer software": ["computer software"],
    "information technology and services": ["information technology and services"],
    "internet": ["internet"],
    "financial services": ["financial services"],
    "banking": ["banking"],
    "marketing and advertising": ["marketing and advertising"],
    "management consulting": ["management consulting"],
    "hospital & health care": ["hospital & health care"],
    "education management": ["education management"],
    "telecommunications": ["telecommunications"],
    "automotive": ["automotive"],
    "consumer electronics": ["consumer electronics"],
    "real estate": ["real estate"],
    "retail": ["retail"],

    # Common user-facing names → Apollo equivalents
    "blockchain": ["computer software", "internet", "financial services"],
    "blockchain/web3": ["computer software", "internet", "financial services"],
    "blockchain & crypto": ["computer software", "internet", "financial services"],
    "web3": ["computer software", "internet", "financial services"],
    "crypto": ["computer software", "internet", "financial services"],
    "cryptocurrency": ["computer software", "internet", "financial services"],
    "defi": ["computer software", "internet", "financial services"],
    "fintech": ["financial services", "computer software", "internet"],
    "saas": ["computer software", "internet", "information technology and services"],
    "software": ["computer software"],
    "tech": ["computer software", "information technology and services", "internet"],
    "technology": ["computer software", "information technology and services", "internet"],
    "it": ["information technology and services"],
    "ai": ["computer software", "information technology and services"],
    "artificial intelligence": ["computer software", "information technology and services"],
    "machine learning": ["computer software", "information technology and services"],
    "edtech": ["education management", "e-learning", "computer software"],
    "e-learning": ["e-learning"],
    "healthtech": ["hospital & health care", "computer software", "medical devices"],
    "health tech": ["hospital & health care", "computer software", "medical devices"],
    "health technology": ["hospital & health care", "computer software", "medical devices"],
    "healthcare": ["hospital & health care", "medical devices", "health, wellness and fitness"],
    "health care": ["hospital & health care", "medical devices", "health, wellness and fitness"],
    "medical": ["hospital & health care", "medical devices", "medical practice"],
    "biotech": ["biotechnology", "pharmaceuticals", "hospital & health care"],
    "biotechnology": ["biotechnology", "pharmaceuticals"],
    "pharma": ["pharmaceuticals", "hospital & health care"],
    "pharmaceuticals": ["pharmaceuticals"],
    "wellness": ["health, wellness and fitness"],
    "fitness": ["health, wellness and fitness"],
    "ecommerce": ["internet", "retail", "computer software"],
    "e-commerce": ["internet", "retail", "computer software"],
    "gaming": ["computer games", "entertainment", "computer software"],
    "media": ["media production", "online media", "entertainment"],
    "advertising": ["marketing and advertising"],
    "marketing": ["marketing and advertising"],
    "consulting": ["management consulting"],
    "telecom": ["telecommunications"],
    "cybersecurity": ["computer & network security", "computer software"],
    "security": ["computer & network security", "computer software"],
    "cloud": ["computer software", "information technology and services"],
    "devops": ["computer software", "information technology and services"],
    "data": ["computer software", "information technology and services"],
    "analytics": ["computer software", "information technology and services"],
}


def normalize_industries(user_industries: Optional[List[str]]) -> Optional[List[str]]:
    """Convert user/LLM industry names to Apollo API industry names.

    Args:
        user_industries: List of industry names from the candidate profile.

    Returns:
        Deduplicated list of Apollo-compatible industry names, or None.
    """
    if not user_industries:
        return None

    seen = set()
    apollo_industries = []

    for industry in user_industries:
        key = industry.strip().lower()
        mapped = _INDUSTRY_MAP.get(key)

        if mapped:
            for ind in mapped:
                if ind not in seen:
                    seen.add(ind)
                    apollo_industries.append(ind)
            logger.info("Industry '%s' → %s", industry, mapped)
        else:
            # Pass through as-is (Apollo may still match)
            if key not in seen:
                seen.add(key)
                apollo_industries.append(industry)
            logger.warning("Industry '%s' not in map, passing through as-is", industry)

    logger.info("Normalized %d user industries to %d Apollo industries: %s",
                len(user_industries), len(apollo_industries), apollo_industries)
    return apollo_industries if apollo_industries else None


# ═══════════════════════════════════════════════════════════════════════════════
# LOCATION MAPPING — normalize common variations to Apollo-friendly names
# ═══════════════════════════════════════════════════════════════════════════════

_LOCATION_MAP = {
    "bengaluru": "Bangalore, Karnataka, India",
    "bangalore": "Bangalore, Karnataka, India",
    "mumbai": "Mumbai, Maharashtra, India",
    "bombay": "Mumbai, Maharashtra, India",
    "delhi": "Delhi, India",
    "new delhi": "Delhi, India",
    "ncr": "Delhi, India",
    "delhi ncr": "Delhi, India",
    "hyderabad": "Hyderabad, Telangana, India",
    "pune": "Pune, Maharashtra, India",
    "chennai": "Chennai, Tamil Nadu, India",
    "madras": "Chennai, Tamil Nadu, India",
    "kolkata": "Kolkata, West Bengal, India",
    "calcutta": "Kolkata, West Bengal, India",
    "gurgaon": "Gurgaon, Haryana, India",
    "gurugram": "Gurgaon, Haryana, India",
    "noida": "Noida, Uttar Pradesh, India",
    "ahmedabad": "Ahmedabad, Gujarat, India",
    "jaipur": "Jaipur, Rajasthan, India",
    "kochi": "Kochi, Kerala, India",
    "thiruvananthapuram": "Thiruvananthapuram, Kerala, India",
    "indore": "Indore, Madhya Pradesh, India",
    "chandigarh": "Chandigarh, India",
    # International cities — North America
    "san francisco": "San Francisco, California, United States",
    "sf": "San Francisco, California, United States",
    "new york": "New York, New York, United States",
    "nyc": "New York, New York, United States",
    "los angeles": "Los Angeles, California, United States",
    "la": "Los Angeles, California, United States",
    "boston": "Boston, Massachusetts, United States",
    "chicago": "Chicago, Illinois, United States",
    "seattle": "Seattle, Washington, United States",
    "austin": "Austin, Texas, United States",
    "toronto": "Toronto, Ontario, Canada",
    "vancouver": "Vancouver, British Columbia, Canada",
    "montreal": "Montreal, Quebec, Canada",

    # UK & Ireland
    "london": "London, England, United Kingdom",
    "manchester": "Manchester, England, United Kingdom",
    "edinburgh": "Edinburgh, Scotland, United Kingdom",
    "dublin": "Dublin, Ireland",

    # France
    "paris": "Paris, France",
    "lyon": "Lyon, France",
    "marseille": "Marseille, France",
    "lille": "Lille, France",
    "toulouse": "Toulouse, France",
    "nice": "Nice, France",
    "bordeaux": "Bordeaux, France",
    "nantes": "Nantes, France",

    # Germany
    "berlin": "Berlin, Germany",
    "munich": "Munich, Bavaria, Germany",
    "münchen": "Munich, Bavaria, Germany",
    "muenchen": "Munich, Bavaria, Germany",
    "frankfurt": "Frankfurt, Germany",
    "hamburg": "Hamburg, Germany",
    "cologne": "Cologne, Germany",
    "köln": "Cologne, Germany",
    "stuttgart": "Stuttgart, Germany",
    "düsseldorf": "Düsseldorf, Germany",
    "dusseldorf": "Düsseldorf, Germany",

    # Benelux
    "amsterdam": "Amsterdam, North Holland, Netherlands",
    "rotterdam": "Rotterdam, Netherlands",
    "the hague": "The Hague, Netherlands",
    "brussels": "Brussels, Belgium",
    "antwerp": "Antwerp, Belgium",
    "luxembourg": "Luxembourg",
    "luxembourg city": "Luxembourg",

    # Iberia
    "madrid": "Madrid, Spain",
    "barcelona": "Barcelona, Spain",
    "valencia": "Valencia, Spain",
    "lisbon": "Lisbon, Portugal",
    "porto": "Porto, Portugal",

    # Italy
    "rome": "Rome, Italy",
    "milan": "Milan, Italy",
    "turin": "Turin, Italy",
    "naples": "Naples, Italy",

    # Nordics
    "stockholm": "Stockholm, Sweden",
    "copenhagen": "Copenhagen, Denmark",
    "oslo": "Oslo, Norway",
    "helsinki": "Helsinki, Finland",

    # Central / Eastern Europe
    "vienna": "Vienna, Austria",
    "wien": "Vienna, Austria",
    "zurich": "Zurich, Switzerland",
    "geneva": "Geneva, Switzerland",
    "warsaw": "Warsaw, Poland",
    "prague": "Prague, Czechia",
    "budapest": "Budapest, Hungary",

    # Asia / Pacific
    "singapore": "Singapore",
    "hong kong": "Hong Kong",
    "tokyo": "Tokyo, Japan",
    "sydney": "Sydney, New South Wales, Australia",
    "melbourne": "Melbourne, Victoria, Australia",

    # Middle East
    "dubai": "Dubai, United Arab Emirates",
    "abu dhabi": "Abu Dhabi, United Arab Emirates",
    "tel aviv": "Tel Aviv, Israel",

    "remote": "Remote",
}


def _llm_clean_locations(raw_locations: List[str]) -> List[str]:
    """Use Azure OpenAI to clean up garbled / misspelled / compound location strings.

    Examples:
      "Paris — Luxembourgh"  → ["Paris", "Luxembourg"]
      "Munchen"              → ["Munich"]
      "NYC, SF"              → ["New York", "San Francisco"]
      "anywhere"             → []  (not a city)

    Returns canonical English city names (no country suffix). Empty list on failure.
    """
    if not raw_locations:
        return []

    try:
        from services.shared.ai.azure_openai_client import generate_json
    except Exception as e:
        logger.warning("LLM client unavailable for location cleaning: %s", e)
        return []

    schema = {
        "type": "object",
        "properties": {
            "cities": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["cities"],
    }

    prompt = (
        "You are normalizing user-entered city names for a job lead search.\n\n"
        f"Input strings (may be misspelled, combined, or non-city values): {raw_locations}\n\n"
        "Rules:\n"
        "1. Split compound entries on em-dashes, slashes, commas, or 'and' "
        "(e.g., 'Paris — Luxembourgh' becomes two cities).\n"
        "2. Fix spelling (e.g., 'Luxembourgh' → 'Luxembourg', 'Munchen' → 'Munich').\n"
        "3. Use the SHORTEST canonical English city name. "
        "'NYC' → 'New York', not 'New York City'. "
        "'SF' → 'San Francisco'. 'München' → 'Munich'. 'Köln' → 'Cologne'.\n"
        "4. Drop entries that are not cities ('Remote', 'Anywhere', 'Europe', 'USA').\n"
        "5. Deduplicate.\n"
        "6. Return city name only — no country, no commas, no extra text.\n\n"
        "Return as JSON: {\"cities\": [\"Paris\", \"Luxembourg\", ...]}"
    )

    try:
        result = generate_json(prompt, schema, temperature=0.0)
        cities = result.get("cities", []) if isinstance(result, dict) else []
        cities = [c.strip() for c in cities if isinstance(c, str) and c.strip()]
        logger.info("LLM cleaned locations %s → %s", raw_locations, cities)
        return cities
    except Exception as e:
        logger.warning("LLM location cleaning failed: %s", e)
        return []


def normalize_locations(user_locations: List[str]) -> List[str]:
    """Convert user location names to Apollo-friendly location strings.

    Two-pass strategy:
      1. Direct map lookup — fast path, no LLM cost.
      2. For anything still unmatched, send through LLM cleaner to fix
         typos / split compound entries, then retry the map lookup.

    Args:
        user_locations: List of location names from the candidate profile.

    Returns:
        Deduplicated list of Apollo-compatible location names.
    """
    if not user_locations:
        return []

    seen = set()
    normalized = []
    unmatched: List[str] = []

    # ── Pass 1: direct map lookup ────────────────────────────────────────
    for loc in user_locations:
        key = loc.strip().lower()
        if key == "remote":
            logger.info("Skipping 'Remote' location (not an Apollo filter)")
            continue

        mapped = _LOCATION_MAP.get(key)
        if mapped and mapped != "Remote":
            if mapped not in seen:
                seen.add(mapped)
                normalized.append(mapped)
            logger.info("Location '%s' → '%s'", loc, mapped)
        else:
            unmatched.append(loc)

    # ── Pass 2: LLM cleanup for anything still unmatched ────────────────
    if unmatched:
        cleaned = _llm_clean_locations(unmatched)
        for loc in cleaned:
            key = loc.strip().lower()
            if key == "remote":
                continue
            mapped = _LOCATION_MAP.get(key)
            if mapped and mapped != "Remote":
                if mapped not in seen:
                    seen.add(mapped)
                    normalized.append(mapped)
                logger.info("LLM-corrected location '%s' → '%s'", loc, mapped)
            else:
                # LLM gave us a city not in the map — pass through with original casing.
                if loc not in seen:
                    seen.add(loc)
                    normalized.append(loc)
                logger.info("LLM-corrected location '%s' passed through (not in map)", loc)

    logger.info("Normalized %d user locations to %d Apollo locations: %s",
                len(user_locations), len(normalized), normalized)
    return normalized