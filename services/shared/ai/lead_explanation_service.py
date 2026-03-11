import json
import os
import asyncio
from typing import Dict, Any, List

from services.ai.azure_openai_client import generate_json
from core.logger import get_logger

logger = get_logger(__name__)

async def generate_explanation_async(
    candidate_json: Dict[str, Any],
    lead_json: Dict[str, Any],
    schema: Dict[str, Any]
) -> Dict[str, Any]:
    """Async wrapper to call the synchronous Azure OpenAI client."""
    # Note: azure_openai_client.generate_json uses `requests` which is blocking.
    # We wrap it in asyncio.to_thread to allow concurrent execution.
    
    prompt = f"""SYSTEM:
You are a recruiter explaining why a hiring manager is relevant for a candidate.
Return exactly three concise bullet points explaining the match.

USER:
Candidate:
{json.dumps(candidate_json, indent=2)}

Lead:
{json.dumps(lead_json, indent=2)}

Company:
{{
  "industry": "{lead_json.get('industry', 'Unknown')}",
  "size": "{lead_json.get('company_size', 'Unknown')}",
  "location": "{lead_json.get('location', 'Unknown')}"
}}

Return JSON only.
"""
    try:
        result = await asyncio.to_thread(
            generate_json,
            prompt=prompt,
            schema=schema,
            temperature=0.3
        )
        # Join bullet points into a single formatted string for the UI
        reasoning_list = result.get("reasoning", [])
        joined_reasoning = "\n".join([f"• {r}" for r in reasoning_list])
        
        # Inject reasoning back into the original lead dict
        lead_json["reasoning"] = joined_reasoning
        return lead_json
    except Exception as e:
        logger.error("[AI_EXPLANATION] Failed for lead %s: %s", lead_json.get("name"), e)
        lead_json["reasoning"] = "• Error generating reasoning."
        return lead_json


async def generate_bulk_explanations(
    candidate_profile: Dict[str, Any],
    leads: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Generate explanations for a final batch of selected leads concurrently.
    
    Args:
        candidate_profile: The user candidate profile JSON.
        leads: A list of dicts representing the top N scored contacts.
        
    Returns:
        The exact same list of leads, but with a `reasoning` key added to each.
    """
    if not leads:
        return []

    logger.info("[AI_EXPLANATION] Starting bulk explanation generation for %d leads", len(leads))
    
    schema_path = os.path.join("app", "schemas", "ai", "lead_explanation_schema.json")
    with open(schema_path, "r") as f:
        schema = json.load(f)

    # Create concurrent tasks
    tasks = [
        generate_explanation_async(candidate_profile, lead, schema)
        for lead in leads
    ]
    
    # Wait for all explanations to generate
    enriched_leads = await asyncio.gather(*tasks)
    
    logger.info("[AI_EXPLANATION] Bulk generation completed")
    return list(enriched_leads)
