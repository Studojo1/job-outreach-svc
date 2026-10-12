"""One bounded JSON call to the email deployment, for draft rewrites.

The shared `azure_openai_client.generate_json` retries three times with a 90 s
timeout each (about 270 s worst case) and logs the raw model output when it is
not JSON, which on this path is the student's email. A rewrite runs inside a
request (route 9) or a chat turn with a 150 s wall clock, so it gets its own
small client: a short timeout, a caller-chosen number of attempts, and logs
that carry only status codes and exception type names.

`drafting` imports `generate_json` from here by name, so tests that patch
`drafting.generate_json` still replace it.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import jsonschema
import requests

from core.config import settings
from core.logger import get_logger
from services.shared.ai.azure_openai_client import ContentFilterError, PermanentAPIError

logger = get_logger(__name__)

TIMEOUT_SECONDS = 50
_JSON_LINE = "Return ONLY raw valid JSON matching the provided schema. Do not wrap in code blocks."


def _strip_fence(content: str) -> str:
    content = (content or "").strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


def _once(url: str, headers: dict, payload: dict, schema: dict, timeout: float) -> dict:
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code == 400:
        try:
            code = (resp.json().get("error") or {}).get("code")
        except Exception:  # noqa: BLE001 - a non-JSON 400 body
            code = None
        if code == "content_filter":
            raise ContentFilterError("content filter")
        raise PermanentAPIError(f"HTTP 400 ({code or 'unknown'})")
    if 400 <= resp.status_code < 500 and resp.status_code != 429:
        raise PermanentAPIError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    parsed = json.loads(_strip_fence(resp.json()["choices"][0]["message"]["content"]))
    jsonschema.validate(instance=parsed, schema=schema)
    return parsed


def generate_json(prompt: str, schema: dict, temperature: float = 0.6, system_prompt: Optional[str] = None,
                  deployment: Optional[str] = None, *, attempts: int = 1,
                  timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """Parsed, schema-valid JSON from one chat completion. Raises
    ContentFilterError (never retried), PermanentAPIError for other 4xx, and
    ValueError once `attempts` tries have failed (timeouts included)."""
    endpoint, version, key = (settings.AZURE_OPENAI_ENDPOINT, settings.AZURE_OPENAI_API_VERSION,
                              settings.AZURE_OPENAI_KEY)
    deployment = deployment or settings.AZURE_OPENAI_EMAIL_DEPLOYMENT
    if not all([endpoint, version, deployment, key]):
        raise ValueError("Azure OpenAI credentials not configured.")
    url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={version}"
    messages = [{"role": "user", "content": f"{prompt}\n\nJSON SCHEMA:\n{json.dumps(schema)}"}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": f"{system_prompt}\n\n{_JSON_LINE}"})
    payload = {"messages": messages, "response_format": {"type": "json_object"}, "temperature": temperature}
    headers = {"Content-Type": "application/json", "api-key": key}
    last: Optional[Exception] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return _once(url, headers, payload, schema, timeout)
        except PermanentAPIError:
            raise
        except Exception as e:  # noqa: BLE001 - timeouts, 5xx, bad JSON: retried, then one ValueError
            last = e
            logger.warning("[AGENT] rewrite call attempt %s failed: %s", attempt, type(e).__name__)
            if attempt < attempts:
                time.sleep(1)
    raise ValueError(f"rewrite failed: {type(last).__name__}") from None
