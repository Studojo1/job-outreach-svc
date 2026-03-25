"""Dodo Payments service — wraps the dodopayments SDK for checkout creation."""

import logging
from typing import Optional

from dodopayments import AsyncDodoPayments

from core.config import settings

logger = logging.getLogger(__name__)


def _get_client() -> AsyncDodoPayments:
    """Create a Dodo Payments async client."""
    return AsyncDodoPayments(
        bearer_token=settings.DODO_PAYMENTS_API_KEY,
        environment="test_mode" if settings.DODO_TEST_MODE else "live_mode",
    )


async def create_checkout(
    product_id: str,
    customer_email: str,
    customer_name: str,
    return_url: str,
    metadata: Optional[dict] = None,
) -> dict:
    """Create a Dodo Payments checkout session.

    Returns:
        {"session_id": str, "checkout_url": str}
    """
    client = _get_client()

    try:
        response = await client.checkout_sessions.create(
            product_cart=[{"product_id": product_id, "quantity": 1}],
            customer={"email": customer_email, "name": customer_name or "Customer"},
            return_url=return_url,
            metadata=metadata or {},
        )

        logger.info(
            "[DODO] Checkout created: session=%s product=%s email=%s",
            response.session_id,
            product_id,
            customer_email,
        )

        return {
            "session_id": response.session_id,
            "checkout_url": response.checkout_url,
        }

    except Exception as e:
        logger.error("[DODO] Checkout creation failed: %s", e)
        raise