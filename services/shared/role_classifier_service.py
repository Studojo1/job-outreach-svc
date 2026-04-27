"""Role classification service.

Maps a candidate's target role to a high-level function category
so the system can select the right title family.
"""

from typing import List

from core.logger import get_logger

logger = get_logger(__name__)

# Lowercase role keywords → function category
_ROLE_FUNCTION_MAP = {
    # Data
    "data analyst": "data",
    "data scientist": "data",
    "data engineer": "data",
    "bi analyst": "data",
    "business intelligence": "data",
    "analytics": "data",
    "data analytics": "data",
    "machine learning": "data",
    "ml engineer": "data",
    "ai engineer": "data",
    "research analyst": "data",
    "supply chain analyst": "data",

    # Business / Strategy / Consulting
    "business analyst": "business",
    "business operations": "business",
    "operations analyst": "business",
    "strategy analyst": "business",
    "strategy consultant": "business",
    "management consultant": "business",
    "management consulting": "business",
    "industry research": "business",
    "industry research associate": "business",
    "consulting": "business",
    "strategy": "business",

    # Product
    "product analyst": "product",
    "product manager": "product",
    "product owner": "product",
    "program manager": "product",
    "technical program manager": "product",

    # Engineering (software)
    "software engineer": "engineering",
    "software developer": "engineering",
    "backend engineer": "engineering",
    "frontend engineer": "engineering",
    "full stack": "engineering",
    "fullstack": "engineering",
    "developer": "engineering",
    "web developer": "engineering",
    "mobile developer": "engineering",
    "ios developer": "engineering",
    "android developer": "engineering",
    "qa engineer": "engineering",
    "test engineer": "engineering",

    # Cloud / DevOps / Platform
    "devops": "cloud_ops",
    "sre": "cloud_ops",
    "site reliability": "cloud_ops",
    "cloud engineer": "cloud_ops",
    "cloud computing": "cloud_ops",
    "cloud architect": "cloud_ops",
    "platform engineer": "cloud_ops",
    "infrastructure engineer": "cloud_ops",
    "systems engineer": "cloud_ops",
    "network engineer": "cloud_ops",

    # Marketing
    "marketing": "marketing",
    "marketing analyst": "marketing",
    "marketing manager": "marketing",
    "marketing intern": "marketing",
    "marketing specialist": "marketing",
    "digital marketing": "marketing",
    "digital marketing specialist": "marketing",
    "growth analyst": "marketing",
    "growth manager": "marketing",
    "growth marketing": "marketing",
    "content strategist": "marketing",
    "seo": "marketing",
    "seo specialist": "marketing",
    "sem": "marketing",
    "social media": "marketing",
    "social media intern": "marketing",
    "social media manager": "marketing",
    "performance marketing": "marketing",
    "brand": "marketing",
    "brand manager": "marketing",

    # Design
    "ux designer": "design",
    "ui designer": "design",
    "ui ux": "design",
    "ux/ui": "design",
    "product designer": "design",
    "graphic designer": "design",
    "interaction designer": "design",
    "designer": "design",
    "visual designer": "design",

    # Sales / BD
    "sales": "sales",
    "sales development": "sales",
    "sdr": "sales",
    "bdr": "sales",
    "sales development representative": "sales",
    "business development representative": "sales",
    "business development": "sales",
    "account executive": "sales",
    "account manager": "sales",
    "partnerships": "sales",

    # Customer Success
    "customer success": "customer_success",
    "customer experience": "customer_success",
    "client success": "customer_success",

    # Civil engineering / Construction
    "civil engineer": "civil_construction",
    "civil engineering": "civil_construction",
    "construction": "civil_construction",
    "construction manager": "civil_construction",
    "structural engineer": "civil_construction",
    "structural engineering": "civil_construction",
    "site engineer": "civil_construction",
    "project engineer": "civil_construction",
    "estimator": "civil_construction",
    "quantity surveyor": "civil_construction",

    # HR / People
    "hr": "hr",
    "hr coordinator": "hr",
    "hr manager": "hr",
    "human resources": "hr",
    "people operations": "hr",
    "people ops": "hr",
    "talent acquisition": "hr",
    "talent manager": "hr",
    "recruiter": "hr",

    # Writing / Content / Editorial
    "writer": "writing_content",
    "content writer": "writing_content",
    "copywriter": "writing_content",
    "editor": "writing_content",
    "editorial": "writing_content",
    "journalist": "writing_content",
    "creative director": "writing_content",
    "content manager": "writing_content",
    "content lead": "writing_content",

    # Finance
    "finance": "finance",
    "finance manager": "finance",
    "financial analyst": "finance",
    "accountant": "finance",
    "accounting": "finance",
    "treasurer": "finance",
    "treasury": "finance",
    "controller": "finance",
    "fp&a": "finance",
    "investment": "finance",

    # Legal
    "legal": "legal",
    "legal counsel": "legal",
    "lawyer": "legal",
    "attorney": "legal",
    "compliance": "legal",
    "general counsel": "legal",

    # Operations / Supply chain / Logistics
    "operations": "operations",
    "operations manager": "operations",
    "supply chain": "operations",
    "logistics": "operations",
    "procurement": "operations",
    "operations associate": "operations",
    "business operations associate": "operations",

    # Security
    "security engineer": "security",
    "security analyst": "security",
    "penetration tester": "security",
    "cybersecurity": "security",
    "infosec": "security",

    # Blockchain
    "blockchain": "blockchain",
    "smart contract": "blockchain",
    "web3": "blockchain",
    "solidity": "blockchain",
    "crypto": "blockchain",
}


def _llm_clean_roles(raw_roles: List[str]) -> List[str]:
    """Use Azure OpenAI to clean up garbled / misspelled / compound role strings.

    Examples:
      "Software Engineer — Ai engineer"  → ["Software Engineer", "AI Engineer"]
      "Civil engineering intern"         → ["Civil Engineer"]
      "Industry Research Associate —     → ["Industry Research Associate", "Strategy Consultant"]
       strategy consulting"
      "anything"                         → []  (not a role)

    Returns canonical English role names. Empty list on failure.
    """
    if not raw_roles:
        return []

    try:
        from services.shared.ai.azure_openai_client import generate_json
    except Exception as e:
        logger.warning("LLM client unavailable for role cleaning: %s", e)
        return []

    schema = {
        "type": "object",
        "properties": {
            "roles": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["roles"],
    }

    prompt = (
        "You are normalizing user-entered job role names for a hiring-manager search.\n\n"
        f"Input strings (may be misspelled, combined, or vague): {raw_roles}\n\n"
        "Rules:\n"
        "1. Split compound entries on em-dashes, slashes, commas, or 'and' "
        "(e.g., 'Software Engineer — AI Engineer' becomes two roles).\n"
        "2. Fix spelling and use canonical professional titles "
        "(e.g., 'civil engg' → 'Civil Engineer', 'cloud computing' → 'Cloud Engineer').\n"
        "3. Drop entries that are not roles ('anything', 'open to anything', 'remote', 'flexible').\n"
        "4. Use the SHORTEST canonical job title — drop seniority adjectives like 'intern' or "
        "'senior' unless essential. 'Civil Engineering Intern' should become 'Civil Engineer'.\n"
        "5. Deduplicate.\n"
        "6. Return job title only — no company, no extra words.\n\n"
        "Return as JSON: {\"roles\": [\"Civil Engineer\", \"Software Engineer\", ...]}"
    )

    try:
        result = generate_json(prompt, schema, temperature=0.0)
        roles = result.get("roles", []) if isinstance(result, dict) else []
        roles = [r.strip() for r in roles if isinstance(r, str) and r.strip()]
        logger.info("LLM cleaned roles %s → %s", raw_roles, roles)
        return roles
    except Exception as e:
        logger.warning("LLM role cleaning failed: %s", e)
        return []


def _direct_classify(role_lower: str):
    """Try the static map first (exact match, then substring). Returns category or None."""
    if not role_lower:
        return None
    if role_lower in _ROLE_FUNCTION_MAP:
        return _ROLE_FUNCTION_MAP[role_lower]
    # Substring match — prefer the longest matching keyword for accuracy.
    matches = [(k, v) for k, v in _ROLE_FUNCTION_MAP.items() if k in role_lower]
    if not matches:
        return None
    matches.sort(key=lambda kv: len(kv[0]), reverse=True)
    return matches[0][1]


def classify_role_function(role: str) -> str:
    """Classify a role string into a function category.

    Pipeline:
      1. Direct map lookup (exact match, then longest-substring match).
      2. If still unmatched, route through LLM cleaner — splits compound
         entries, fixes typos, returns canonical names — then retry.
      3. If everything fails, default to ``"business"`` and warn so we
         can spot new niche roles to add to the map.
    """
    if not role:
        return "business"

    role_lower = role.strip().lower()

    direct = _direct_classify(role_lower)
    if direct:
        logger.info("Role '%s' classified as '%s' (direct match)", role, direct)
        return direct

    # LLM fallback — handles em-dash compounds, typos, and "Civil engineering intern"-style strings
    cleaned = _llm_clean_roles([role])
    for clean_role in cleaned:
        clean_lower = clean_role.strip().lower()
        result = _direct_classify(clean_lower)
        if result:
            logger.info("Role '%s' → cleaned '%s' → classified as '%s'", role, clean_role, result)
            return result

    logger.warning("Role '%s' could not be classified after LLM cleanup; defaulting to 'business'", role)
    return "business"
