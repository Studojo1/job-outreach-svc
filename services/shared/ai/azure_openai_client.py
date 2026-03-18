"""Azure OpenAI Client — Reusable client for JSON-structured LLM generation.

Handles connection to Azure OpenAI, prompt formatting, JSON parsing,
schema validation, and automatic retries.
"""

import json
import time
from typing import Dict, Any

import requests
import jsonschema

from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds


def generate_json(prompt: str, schema: Dict[str, Any], temperature: float = 0.0) -> Dict[str, Any]:
    """Send a prompt to Azure OpenAI and return parsed, validated JSON.
    
    Args:
        prompt: The full prompt string (system + user instructions).
        schema: A valid JSON Schema dictionary to validate the response.
        temperature: Creativity parameter (default 0.0 for deterministic).
        
    Returns:
        A dictionary containing the parsed and validated JSON response.
        
    Raises:
        ValueError: If the API fails or validation fails after MAX_RETRIES.
    """
    endpoint = settings.AZURE_OPENAI_ENDPOINT
    api_version = settings.AZURE_OPENAI_API_VERSION
    deployment = settings.AZURE_OPENAI_LLM_DEPLOYMENT
    api_key = settings.AZURE_OPENAI_KEY

    if not all([endpoint, api_version, deployment, api_key]):
        logger.error("[AI] Azure OpenAI credentials not fully configured.")
        raise ValueError("Azure OpenAI credentials not configured.")

    # Format the endpoint URL for chat completions
    url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    
    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,
    }
    
    # Append the schema string requirement to the prompt
    full_prompt = f"{prompt}\n\nIMPORTANT: You must return ONLY raw valid JSON that strictly conforms to the following JSON Schema.\nDo not wrap it in ```json codeblocks. Return the raw `{{}}` object.\n\nJSON SCHEMA:\n{json.dumps(schema, indent=2)}"

    payload = {
        "messages": [
            {"role": "user", "content": full_prompt}
        ],
        "response_format": {"type": "json_object"}
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("[AI] Sending request to Azure OpenAI (attempt %d/%d)", attempt, MAX_RETRIES)
            resp = requests.post(url, headers=headers, json=payload, timeout=45)
            
            if not resp.ok:
                logger.error("[AI] HTTP Error %d: %s", resp.status_code, resp.text)
                
            resp.raise_for_status()
            
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            
            # Robust markdown code block stripping
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            # Parse JSON
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning("[AI] Failed to parse JSON response: %s (Response: %s)", e, content)
                raise ValueError(f"Invalid JSON returned: {e}")
                
            # Validate against schema
            try:
                jsonschema.validate(instance=parsed, schema=schema)
                # Log usage stats and key output fields for debugging
                usage = data.get("usage", {})
                logger.info("[AI_OUTPUT] tokens_in=%d, tokens_out=%d, total=%d, keys=%s",
                            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                            usage.get("total_tokens", 0), list(parsed.keys()))
                logger.info("[AI] Successfully generated and validated JSON payload.")
                return parsed
            except jsonschema.ValidationError as e:
                logger.warning("[AI] Schema validation failed: %s", e.message)
                raise ValueError(f"Schema validation failed: {e.message}")
                
        except Exception as e:
            logger.error("[AI] Attempt %d failed: %s", attempt, str(e))
            if attempt < MAX_RETRIES:
                logger.info("[AI] Retrying in %d seconds...", RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                logger.error("[AI] All %d attempts failed.", MAX_RETRIES)
                raise ValueError(f"AI generation failed after {MAX_RETRIES} attempts. Last error: {str(e)}")

    raise ValueError("AI generation failed unexpectedly.")
