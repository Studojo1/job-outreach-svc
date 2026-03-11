"""JWT Service — Token generation and verification for user authentication."""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import jwt

from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)


def create_access_token(user_id: int, email: str) -> str:
    """Create a JWT access token for a user.

    Args:
        user_id: The user's database ID.
        email: The user's email address.

    Returns:
        Encoded JWT string.
    """
    expire = datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRY_HOURS)
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    logger.info("Created JWT for user_id=%d", user_id)
    return token


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a JWT token.

    Args:
        token: The JWT string to verify.

    Returns:
        Decoded payload dict with 'sub' (user_id) and 'email', or None if invalid.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid JWT token: %s", e)
        return None
