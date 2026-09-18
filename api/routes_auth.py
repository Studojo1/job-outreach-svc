"""Authentication Routes — BetterAuth session validation only."""

import base64
import json
import time

from fastapi import APIRouter, Depends, Request

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
    """Debug: attempt Voyager identity lookup and return raw status + any URNs found."""
    import re as _re
    import httpx
    from sqlalchemy.orm import Session as _Session
    from database.session import get_db as _get_db
    from database.models import LinkedInToken
    from services.linkedin_outreach.crypto import decrypt, decrypt_second
    from core.config import settings as _settings

    # Use dependency-injected session pattern to avoid raw SessionLocal issues
    from database.session import SessionLocal as _SL
    db = _SL()
    result: dict = {"slug": slug}
    try:
        token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
        if not token_row:
            return {"error": "No LinkedIn token stored"}

        try:
            li_at = decrypt(token_row.li_at_enc, token_row.nonce)
            jsessionid = decrypt_second(token_row.jsessionid_enc, token_row.nonce)
            result["li_at_prefix"] = li_at[:12] + "..."
            result["decrypt_ok"] = True
        except Exception as e:
            return {"error": f"Decrypt failed: {e}"}

        proxy_url = (_settings.LINKEDIN_PROXY_URL or "").strip()
        result["proxy_configured"] = bool(proxy_url)
        result["proxy_preview"] = proxy_url[:30] + "..." if proxy_url else None

        proxy_kwarg = {"proxy": proxy_url} if proxy_url else {}

        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        hdrs = {
            "Cookie": f"li_at={li_at}; JSESSIONID={jsessionid}",
            "csrf-token": jsessionid,
            "x-restli-protocol-version": "2.0.0",
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "User-Agent": ua,
        }

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, **proxy_kwarg) as client:
                r = await client.get(
                    f"https://www.linkedin.com/voyager/api/identity/profiles/{slug}",
                    headers=hdrs,
                )
            urns = _re.findall(r"fsd_profile:([A-Za-z0-9_-]{20,})", r.text)
            result["voyager_status"] = r.status_code
            result["urns_found"] = urns[:3]
            result["response_preview"] = r.text[:400]
        except Exception as e:
            result["voyager_error"] = str(e)

    except Exception as e:
        result["error"] = str(e)
    finally:
        db.close()

    return result


@router.get("/me")
def auth_me(current_user: User = Depends(get_current_user)):
    """Return current authenticated user info."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "avatar_url": current_user.image,
    }
