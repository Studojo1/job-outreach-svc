"""Authentication Routes — BetterAuth session validation only."""

import base64
import json
import time

from fastapi import APIRouter, Depends

from database.models import User
from api.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _make_jwt(user: User) -> str:
    """Mint a short-lived HS256 JWT cached in the frontend's localStorage.

    Uses LINKEDIN_ENCRYPTION_KEY as HMAC secret (both are AES-256 = 32 bytes,
    which is also a valid HMAC-SHA256 key). TTL: 24 hours.
    """
    import hmac, hashlib
    from core.config import settings

    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": user.id,
        "email": user.email,
        "exp": int(time.time()) + 86400,
    }).encode()).rstrip(b"=").decode()

    secret = base64.b64decode(settings.LINKEDIN_ENCRYPTION_KEY)
    sig_bytes = hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


@router.get("/token")
def get_token(current_user: User = Depends(get_current_user)):
    """Exchange a valid BetterAuth session cookie for a short-lived JWT.

    The frontend's ensureAuthToken() calls this as a fallback when the main
    platform's /api/auth/token endpoint is unavailable (e.g. cross-origin staging).
    """
    return {"token": _make_jwt(current_user)}


@router.get("/me")
def auth_me(current_user: User = Depends(get_current_user)):
    """Return current authenticated user info."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "avatar_url": current_user.image,
    }
