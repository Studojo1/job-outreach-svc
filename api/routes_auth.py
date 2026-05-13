"""Authentication Routes — BetterAuth session validation only."""

import base64
import json
import time

from fastapi import APIRouter, Depends, Request

from database.models import User
from api.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])

_JWT_SECRET = None  # lazy-loaded from settings


def _make_jwt(user: User) -> str:
    """Mint a short-lived HS256 JWT for the frontend localStorage token cache.

    We don't have a signing key shared with the main platform, so we use the
    app's LINKEDIN_ENCRYPTION_KEY as HMAC secret. The frontend only uses this
    token as an Authorization: Bearer header — the backend validates it by
    decoding the sub claim and looking up the user, same as get_admin_user.
    TTL: 24 hours.
    """
    import hmac, hashlib, os
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


@router.get("/debug-cookies")
def debug_cookies(request: Request):
    """Temporary endpoint to inspect incoming cookies."""
    return {
        "cookies": dict(request.cookies),
        "cookie_header": request.headers.get("cookie", ""),
        "origin": request.headers.get("origin", ""),
        "referer": request.headers.get("referer", ""),
    }


@router.get("/me")
def auth_me(current_user: User = Depends(get_current_user)):
    """Return current authenticated user info."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "avatar_url": current_user.image,
    }
