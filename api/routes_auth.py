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


@router.get("/debug-voyager")
async def debug_voyager(slug: str = "williamhgates", current_user: User = Depends(get_current_user)):
    """Debug: attempt Voyager identity lookup for a slug and return raw response.

    Reveals whether the LinkedIn session / proxy / Voyager API is working.
    """
    import httpx
    from database.session import SessionLocal
    from database.models import LinkedInToken
    from services.linkedin_outreach.crypto import decrypt, decrypt_second
    from services.linkedin_outreach.automation_service import _headers, _proxy

    db = SessionLocal()
    try:
        token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
        if not token_row:
            return {"error": "No LinkedIn token found — connect LinkedIn first"}

        li_at = decrypt(token_row.li_at_enc, token_row.nonce)
        jsessionid = decrypt_second(token_row.jsessionid_enc, token_row.nonce)

        proxy_cfg = _proxy(token_row.proxy_session_id)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, **proxy_cfg) as client:
            r = await client.get(
                f"https://www.linkedin.com/voyager/api/identity/profiles/{slug}",
                headers=_headers(li_at, jsessionid),
            )

        body_preview = r.text[:500]
        import re as _re
        urns = _re.findall(r"fsd_profile:([A-Za-z0-9_-]{20,})", r.text)

        return {
            "slug": slug,
            "status": r.status_code,
            "proxy_configured": bool(proxy_cfg),
            "proxy_url_preview": str(list(proxy_cfg.values())[0])[:30] + "..." if proxy_cfg else None,
            "urns_found": urns[:3],
            "li_at_prefix": li_at[:10] + "...",
            "response_preview": body_preview,
        }
    finally:
        db.close()


@router.get("/me")
def auth_me(current_user: User = Depends(get_current_user)):
    """Return current authenticated user info."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "avatar_url": current_user.image,
    }
