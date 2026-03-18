"""JWT Service — BetterAuth JWKS-based token verification."""

import time
import threading
from typing import Optional, Dict, Any

import jwt
import httpx

from core.logger import get_logger

logger = get_logger(__name__)

JWKS_URL = "http://frontend:3000/api/auth/jwks"
JWKS_CACHE_TTL = 300  # 5 minutes

_jwks_cache: Optional[Dict[str, Any]] = None
_jwks_cache_expires: float = 0
_jwks_lock = threading.Lock()


def _fetch_jwks() -> Dict[str, Any]:
    """Fetch JWKS from BetterAuth and return the key set."""
    global _jwks_cache, _jwks_cache_expires

    now = time.time()
    if _jwks_cache and now < _jwks_cache_expires:
        return _jwks_cache

    with _jwks_lock:
        # Double-check after acquiring lock
        if _jwks_cache and time.time() < _jwks_cache_expires:
            return _jwks_cache

        try:
            response = httpx.get(JWKS_URL, timeout=10)
            response.raise_for_status()
            jwks_data = response.json()
            _jwks_cache = jwks_data
            _jwks_cache_expires = time.time() + JWKS_CACHE_TTL
            logger.info("JWKS fetched and cached from %s", JWKS_URL)
            return jwks_data
        except Exception as e:
            logger.error("Failed to fetch JWKS from %s: %s", JWKS_URL, e)
            # Return stale cache if available
            if _jwks_cache:
                logger.warning("Using stale JWKS cache")
                return _jwks_cache
            raise


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify a BetterAuth JWT using the JWKS endpoint (RS256).

    Returns:
        Decoded payload dict with 'sub' (user_id), or None if invalid.
    """
    try:
        jwks_data = _fetch_jwks()
        public_keys = {}

        for key_data in jwks_data.get("keys", []):
            kid = key_data.get("kid")
            if kid:
                public_keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        # Decode header to find the key ID
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        if not kid or kid not in public_keys:
            logger.warning("JWT kid=%s not found in JWKS", kid)
            return None

        payload = jwt.decode(
            token,
            key=public_keys[kid],
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        return payload

    except jwt.ExpiredSignatureError:
        logger.warning("BetterAuth JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid BetterAuth JWT: %s", e)
        return None
    except Exception as e:
        logger.error("JWT verification error: %s", e)
        return None
