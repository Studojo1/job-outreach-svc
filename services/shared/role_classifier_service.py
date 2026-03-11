"""Role classification service.

Maps a candidate's target role to a high-level function category
so the system can select the right title family.
"""

from core.logger import get_logger

logger = get_logger(__name__)

# Lowercase role keywords → function category
_ROLE_FUNCTION_MAP = {
    # Data
    "data analyst": "data",
    "data scientist": "data",
    "data engineer": "data",
    "bi analyst": "data",
    "analytics": "data",
    "machine learning": "data",
    "ml engineer": "data",
    "ai engineer": "data",
    # Business
    "business analyst": "business",
    "business operations": "business",
    "operations analyst": "business",
    "strategy analyst": "business",
    "management consultant": "business",
    "business development": "business",
    # Product
    "product analyst": "product",
    "product manager": "product",
    "product designer": "product",
    "program manager": "product",
    # Engineering
    "software engineer": "engineering",
    "backend engineer": "engineering",
    "frontend engineer": "engineering",
    "full stack": "engineering",
    "devops": "engineering",
    "sre": "engineering",
    "developer": "engineering",
    "web developer": "engineering",
    "mobile developer": "engineering",
    "ios developer": "engineering",
    "android developer": "engineering",
    # Marketing
    "marketing analyst": "marketing",
    "growth analyst": "marketing",
    "content strategist": "marketing",
    "digital marketing": "marketing",
    "growth manager": "marketing",
    # Design
    "ux designer": "design",
    "ui designer": "design",
    "product designer": "design",
    "graphic designer": "design",
    "interaction designer": "design",
    # Security / Blockchain
    "security engineer": "security",
    "security analyst": "security",
    "penetration tester": "security",
    "blockchain": "blockchain",
    "smart contract": "blockchain",
    "web3": "blockchain",
    "solidity": "blockchain",
    "crypto": "blockchain",
}


def classify_role_function(role: str) -> str:
    """Classify a role string into a function category.

    Args:
        role: The candidate's target role, e.g. ``"Data Analyst"``.

    Returns:
        One of the function categories. Defaults to ``"business"``
        when no match is found.
    """
    role_lower = role.strip().lower()

    # Exact match
    if role_lower in _ROLE_FUNCTION_MAP:
        result = _ROLE_FUNCTION_MAP[role_lower]
        logger.info("Role '%s' classified as '%s' (exact match)", role, result)
        return result

    # Substring match
    for keyword, function in _ROLE_FUNCTION_MAP.items():
        if keyword in role_lower:
            logger.info("Role '%s' classified as '%s' (substring match on '%s')", role, function, keyword)
            return function

    logger.info("Role '%s' could not be classified, defaulting to 'business'", role)
    return "business"