import json
import os
from typing import Dict, Any

from job_outreach_tool.services.ai.azure_openai_client import generate_json
from job_outreach_tool.core.logger import get_logger

logger = get_logger(__name__)

def evaluate_role_intelligence(candidate_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Convert candidate profile data into hiring intelligence using Azure OpenAI.

    Args:
        candidate_profile: The normalized candidate JSON data.

    Returns:
        Dict matching the role_intelligence_schema.json format.
    """
    logger.info("[AI_ROLE_INTEL] Starting candidate role intelligence evaluation")
    
    # Load schema
    schema_path = os.path.join("app", "schemas", "ai", "role_intelligence_schema.json")
    with open(schema_path, "r") as f:
        schema = json.load(f)

    prompt = f"""SYSTEM:
You are an expert recruiter and labor market analyst.
Your task is to analyze a candidate profile and determine which hiring managers would be responsible for hiring that candidate.
Only return valid JSON matching the provided schema.
Never include explanations.

USER:
Candidate profile JSON:
{json.dumps(candidate_profile, indent=2)}

Determine:
1. The departments this candidate belongs to
2. The hiring manager titles responsible for hiring them
3. Industry expansions related to the candidate's interests
4. Company size ranges where these roles commonly exist
5. Appropriate hiring seniorities

CRITICAL INSTRUCTIONS:
- You must generate HIRING MANAGERS ONLY (e.g., "VP of Engineering", "Head of Product", "Marketing Director").
- DO NOT INCLUDE HR TITLES. Completely exclude titles like "Head of Talent", "Talent Acquisition", "Recruiter", "People Operations", or "HR Manager".
- Return strictly valid JSON matching the schema.
"""

    result = generate_json(prompt=prompt, schema=schema, temperature=0.1)
    logger.info("[AI_ROLE_INTEL] Successfully generated role intelligence")
    return result
