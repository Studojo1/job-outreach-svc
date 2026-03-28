"""Reply Classifier Service — Classify email reply sentiment using Azure OpenAI.

Classifies replies to outreach emails as positive, negative, or neutral.
Uses the existing generate_json() function for consistency with the rest of the AI pipeline.
"""

from typing import Dict, Any

from core.logger import get_logger
from services.shared.ai.azure_openai_client import generate_json

logger = get_logger(__name__)

# JSON Schema for the classification response
SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {
            "type": "string",
            "enum": ["positive", "negative", "neutral"],
        },
        "summary": {
            "type": "string",
            "description": "One-sentence summary of the reply intent",
        },
    },
    "required": ["sentiment", "summary"],
}

CLASSIFICATION_PROMPT = """You are classifying an email reply to a job outreach message.

The original outreach email was sent by a job seeker to a hiring manager, recruiter, or professional at a company.
The recipient has replied. Classify their reply sentiment:

- **positive**: The recipient is interested, open to a conversation, wants to learn more, or is receptive.
  Examples: "Let's chat", "Send me your resume", "Interesting, tell me more", "Happy to connect", "I'll forward this to our team".

- **negative**: The recipient is declining, not interested, asks to be removed, or is clearly annoyed.
  Examples: "Not interested", "Please don't email me", "We're not hiring", "Remove me from your list", "This is spam".

- **neutral**: The reply is ambiguous, a simple acknowledgment, an auto-reply, or doesn't clearly indicate interest or disinterest.
  Examples: "Thanks for reaching out", "I'll take a look", "Who is this?", "Out of office" auto-replies, "Got it".

Reply text:
---
{reply_text}
---

Respond with a JSON object containing 'sentiment' and 'summary'."""


def classify_reply_sentiment(reply_text: str) -> Dict[str, Any]:
    """Classify the sentiment of an email reply.

    Args:
        reply_text: The plain text body of the reply email.

    Returns:
        Dict with keys: sentiment (positive/negative/neutral), summary (str).
        On failure, returns {"sentiment": "neutral", "summary": "Classification failed"}.
    """
    if not reply_text or len(reply_text.strip()) < 3:
        return {"sentiment": "neutral", "summary": "Empty or very short reply"}

    # Truncate very long replies to save tokens (3000 chars is plenty for intent)
    truncated = reply_text[:3000]

    try:
        prompt = CLASSIFICATION_PROMPT.replace("{reply_text}", truncated)
        result = generate_json(prompt=prompt, schema=SENTIMENT_SCHEMA, temperature=0.0)
        logger.info("[REPLY_CLASSIFIER] sentiment=%s — %s",
                    result.get("sentiment"), result.get("summary"))
        return result
    except Exception as e:
        logger.error("[REPLY_CLASSIFIER] Classification failed: %s", e)
        return {"sentiment": "neutral", "summary": "Classification failed"}
