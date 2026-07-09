"""Batched LLM calls for pipeline stages (extraction, scoring).

The pipeline calls the model with SMALL, per-stage prompts adjacent to the
data (run-36 forensics: rules evaluated 20+ turns from the evidence get
ignored). JSON in, JSON out, one corrective retry on parse failure.
Config/client imports are lazy so unit tests import this module without env.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)


def _client():
    from openai import AzureOpenAI
    from core.config import settings
    return AzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    ), settings.AZURE_OPENAI_LLM_DEPLOYMENT


def extract_json(text_out: str):
    """Parse the first JSON array/object out of a model reply (fences and
    prose tolerated). Raises ValueError when nothing parseable exists."""
    t = (text_out or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    start = min((i for i in (t.find("["), t.find("{")) if i >= 0), default=-1)
    if start == -1:
        raise ValueError("no JSON in model output")
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(t[start:])
    return obj


def chat_json(system: str, user: str, max_tokens: int = 6000):
    """One batched judgment call. Retries ONCE with a corrective nudge if the
    reply is not parseable JSON; then raises."""
    client, deployment = _client()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in (1, 2):
        resp = client.chat.completions.create(
            model=deployment, messages=messages, max_completion_tokens=max_tokens,
        )
        out = resp.choices[0].message.content or ""
        try:
            return extract_json(out)
        except (ValueError, json.JSONDecodeError):
            if attempt == 2:
                raise
            logger.warning("[BOB/PIPE] non-JSON model reply, retrying once")
            messages.append({"role": "assistant", "content": out[:2000]})
            messages.append({"role": "user", "content": "Reply with ONLY the JSON, no prose, no fences."})
