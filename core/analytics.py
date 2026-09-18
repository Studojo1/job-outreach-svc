"""PostHog analytics — singleton client + fire-and-forget capture helper."""

import logging
from core.config import settings

logger = logging.getLogger(__name__)

_client = None

def _get_client():
    global _client
    if _client is None and settings.POSTHOG_KEY:
        try:
            from posthog import Posthog
            _client = Posthog(
                project_api_key=settings.POSTHOG_KEY,
                host=settings.POSTHOG_HOST,
            )
            logger.info("[PostHog] Client initialised (host=%s)", settings.POSTHOG_HOST)
        except Exception as e:
            logger.warning("[PostHog] Failed to initialise client: %s", e)
    return _client


def capture(event: str, user_id: str, properties: dict = None):
    """Fire a PostHog event. No-op if POSTHOG_KEY is not configured."""
    client = _get_client()
    if not client:
        return
    try:
        client.capture(
            distinct_id=str(user_id),
            event=event,
            properties=properties or {},
        )
    except Exception as e:
        logger.warning("[PostHog] capture failed for event=%s: %s", event, e)


def identify(user_id: str, properties: dict):
    """Set persistent user properties on a PostHog profile."""
    client = _get_client()
    if not client:
        return
    try:
        client.capture(
            distinct_id=str(user_id),
            event="$set",
            properties={"$set": properties},
        )
    except Exception as e:
        logger.warning("[PostHog] identify failed for user=%s: %s", user_id, e)


def shutdown():
    """Flush remaining events on app shutdown."""
    if _client:
        try:
            _client.shutdown()
        except Exception:
            pass
