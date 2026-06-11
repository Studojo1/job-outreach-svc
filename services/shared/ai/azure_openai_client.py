"""Azure OpenAI Client — Reusable client for JSON-structured LLM generation.

Handles connection to Azure OpenAI, prompt formatting, JSON parsing,
schema validation, and automatic retries.
"""

import json
import time
from typing import Dict, Any, Optional

import requests
import jsonschema

from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds


class PermanentAPIError(ValueError):
    """Raised for Azure API errors that will never succeed on retry (4xx responses)."""


class ContentFilterError(PermanentAPIError):
    """Raised specifically when Azure's responsible-AI content filter blocks a prompt."""


def generate_json(
    prompt: str,
    schema: Dict[str, Any],
    temperature: float = 0.0,
    system_prompt: Optional[str] = None,
    deployment: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a prompt to Azure OpenAI and return parsed, validated JSON.

    Args:
        prompt: The user-facing prompt (context data, structure, synthesis).
        schema: A valid JSON Schema dictionary to validate the response.
        temperature: Creativity parameter (default 0.0 for deterministic).
        system_prompt: Optional system-role instructions. When provided, moves all
            instruction/rule content to the system message so the user message
            contains only context data — avoids Azure jailbreak false positives.
        deployment: Override the default LLM deployment. Pass
            settings.AZURE_OPENAI_FAST_DEPLOYMENT for batch/pattern tasks
            that don't need a reasoning model (~17x cheaper).

    Returns:
        A dictionary containing the parsed and validated JSON response.

    Raises:
        ContentFilterError: If Azure's content filter blocks the prompt. Never retried.
        PermanentAPIError: If Azure rejects the request with HTTP 4xx. Never retried.
        ValueError: If the API fails after MAX_RETRIES.
    """
    endpoint = settings.AZURE_OPENAI_ENDPOINT
    api_version = settings.AZURE_OPENAI_API_VERSION
    deployment = deployment or settings.AZURE_OPENAI_LLM_DEPLOYMENT
    api_key = settings.AZURE_OPENAI_KEY

    if not all([endpoint, api_version, deployment, api_key]):
        logger.error("[AI] Azure OpenAI credentials not fully configured.")
        raise ValueError("Azure OpenAI credentials not configured.")

    url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,
    }

    json_instruction = (
        "Return ONLY raw valid JSON matching the provided schema. "
        "Do not wrap in ```json code blocks."
    )

    if system_prompt:
        messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{json_instruction}"},
            {"role": "user", "content": f"{prompt}\n\nJSON SCHEMA:\n{json.dumps(schema, indent=2)}"},
        ]
    else:
        full_prompt = (
            f"{prompt}\n\nIMPORTANT: You must return ONLY raw valid JSON that strictly conforms to "
            f"the following JSON Schema.\nDo not wrap it in ```json codeblocks. Return the raw {{}} object."
            f"\n\nJSON SCHEMA:\n{json.dumps(schema, indent=2)}"
        )
        messages = [{"role": "user", "content": full_prompt}]

    payload = {
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("[AI] Sending request to Azure OpenAI (attempt %d/%d)", attempt, MAX_RETRIES)
            resp = requests.post(url, headers=headers, json=payload, timeout=90)

            if not resp.ok:
                logger.error("[AI] HTTP Error %d: %s", resp.status_code, resp.text[:500])

            if resp.status_code == 400:
                try:
                    err = resp.json().get("error", {})
                except Exception:
                    err = {}
                if err.get("code") == "content_filter":
                    logger.warning("[AI] Content filter blocked prompt. Aborting without retry.")
                    raise ContentFilterError(
                        f"Azure content filter blocked prompt: {err.get('message', 'jailbreak detected')}"
                    )
                raise PermanentAPIError(
                    f"Azure rejected prompt (HTTP 400, code={err.get('code', 'unknown')}): {resp.text[:300]}"
                )

            resp.raise_for_status()

            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning("[AI] Failed to parse JSON response: %s (Response: %s)", e, content)
                raise ValueError(f"Invalid JSON returned: {e}")

            try:
                jsonschema.validate(instance=parsed, schema=schema)
                usage = data.get("usage", {})
                logger.info(
                    "[AI_OUTPUT] tokens_in=%d, tokens_out=%d, total=%d, keys=%s",
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    usage.get("total_tokens", 0),
                    list(parsed.keys()),
                )
                logger.info("[AI] Successfully generated and validated JSON payload.")
                return parsed
            except jsonschema.ValidationError as e:
                logger.warning("[AI] Schema validation failed: %s", e.message)
                raise ValueError(f"Schema validation failed: {e.message}")

        except PermanentAPIError:
            raise  # 4xx errors never succeed on retry
        except Exception as e:
            logger.error("[AI] Attempt %d failed: %s", attempt, str(e))
            if attempt < MAX_RETRIES:
                logger.info("[AI] Retrying in %d seconds...", RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                logger.error("[AI] All %d attempts failed.", MAX_RETRIES)
                raise ValueError(f"AI generation failed after {MAX_RETRIES} attempts. Last error: {str(e)}")

    raise ValueError("AI generation failed unexpectedly.")
