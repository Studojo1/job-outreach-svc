import json
import os
from typing import Dict, Any

from services.ai.azure_openai_client import generate_json
from core.logger import get_logger

logger = get_logger(__name__)

# List of valid Apollo Industries based on standard Apollo taxonomy
VALID_APOLLO_INDUSTRIES = {
    "computer software",
    "internet",
    "information technology & services",
    "marketing & advertising",
    "marketing and advertising",
    "e-learning",
    "financial services",
    "hospital & health care",
    "health, wellness and fitness",
    "management consulting",
    "design",
    "human resources",
    "retail",
    "consumer services",
    "consumer goods",
    "telecommunications",
    "construction",
    "real estate",
    "education management",
    "accounting"
}

# The only allowed keys per the pipeline requirements
PERMITTED_APOLLO_KEYS = {
    "person_titles",
    "person_locations",
    "organization_industries",
    "organization_num_employees_ranges",
    "person_seniorities"
}

from services.ai.azure_openai_client import generate_json
from services.ai.filter_calibration_ai import _classify_titles_by_cluster, _classify_titles_by_seniority
from services.ai.hiring_authority_service import get_hiring_titles
from core.logger import get_logger

logger = get_logger(__name__)

def generate_ai_apollo_filters(
    role_intelligence: Dict[str, Any], candidate_preferences: Dict[str, Any]
) -> Dict[str, Any]:
    """Generate optimized Apollo people search filters from hiring intelligence.

    Args:
        role_intelligence: The JSON output from the role_intelligence_service.
        candidate_preferences: Specific constraints from the candidate (e.g., location).

    Returns:
        Dict matching the apollo_filter_schema.json format.
    """
    logger.info("[AI_FILTER_GEN] Generating Apollo discovery filters")
    
    # Load schema
    schema_path = os.path.join("app", "schemas", "ai", "apollo_filter_schema.json")
    with open(schema_path, "r") as f:
        schema = json.load(f)

    prompt = f"""SYSTEM:
You are an Apollo.io search optimization expert.
Your task is to convert hiring intelligence into optimized Apollo people search filters.
Return only valid JSON matching the schema.

USER:
Role intelligence JSON:
{json.dumps(role_intelligence, indent=2)}

Candidate preferences:
{json.dumps(candidate_preferences, indent=2)}

Generate filters that maximize discovery of hiring managers.

Rules:
- Titles must be decision-makers
- Expand industries intelligently
- Avoid over-filtering
- Respect candidate location preferences
- Use employee ranges appropriately

Return JSON only.
"""

    raw_result = generate_json(prompt=prompt, schema=schema)
    
    # ── Permitted Keys Enforcer ──
    # We must explicitly strip out anything the AI hallucinates, particularly person_departments
    result = {}
    for key in PERMITTED_APOLLO_KEYS:
        if key in raw_result:
            result[key] = raw_result[key]
            
    # If the AI somehow omitted a required base array, ensure it exists
    for key in ["person_titles", "organization_industries"]:
        if key not in result:
            result[key] = []
    
    # ── Deterministic Rule Overrides (Step 2) ──
    # 1. Titles from Hiring Authority
    candidate_role = candidate_preferences.get("preferred_role") or role_intelligence.get("hiring_roles", ["Marketing Specialist"])[0]
    result["person_titles"] = get_hiring_titles(candidate_role)
    
    # 2. Location (Candidate City only)
    candidate_city = (candidate_preferences.get("location_preferences") or ["India"])[0]
    result["person_locations"] = [candidate_city]
    
    # 3. Company Size (11-1000)
    result["organization_num_employees_ranges"] = ["11,50", "51,200", "201,1000"]
    
    # 4. Seniority
    result["person_seniorities"] = ["manager", "head"]
    
    # 5. Industries (NEVER SEND IN INITIAL QUERY)
    result.pop("organization_industries", None)
    
    # Org locations cap
    org_locs_len = len(result.get("organization_locations", []))
    if org_locs_len > 10:
        result["organization_locations"] = result.get("organization_locations", [])[:10]
    
    logger.info("[AI_FILTER_GEN] Successfully generated & validated Apollo filters")
    return result
