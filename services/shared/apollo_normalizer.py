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
    # International cities
    "san francisco": "San Francisco, California, United States",
    "sf": "San Francisco, California, United States",
    "new york": "New York, New York, United States",
    "nyc": "New York, New York, United States",
    "london": "London, England, United Kingdom",
    "singapore": "Singapore",
    "dubai": "Dubai, United Arab Emirates",
    "toronto": "Toronto, Ontario, Canada",
    "berlin": "Berlin, Germany",
    "amsterdam": "Amsterdam, North Holland, Netherlands",
    "remote": "Remote",
}


def normalize_locations(user_locations: List[str]) -> List[str]:
    """Convert user location names to Apollo-friendly location strings.

    Args:
        user_locations: List of location names from the candidate profile.

    Returns:
        Deduplicated list of Apollo-compatible location names.
    """
    if not user_locations:
        return []

    seen = set()
    normalized = []

    for loc in user_locations:
        key = loc.strip().lower()
        # Skip "remote" — it's not a filterable Apollo location
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
            # Pass through as-is
            if loc not in seen:
                seen.add(loc)
                normalized.append(loc)
            logger.info("Location '%s' passed through as-is", loc)

    logger.info("Normalized %d user locations to %d Apollo locations: %s",
                len(user_locations), len(normalized), normalized)
    return normalized