"""API Dependencies — Shared FastAPI dependencies for route handlers."""

import hmac
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.config import settings
from database.session import get_db
from database.models import BetterAuthSession, User

logger = logging.getLogger(__name__)


COOKIE_NAMES = [
    "__Secure-better-auth.session_token",
    "better-auth.session_token",
]

INTERNAL_SECRET_HEADER = "x-studojo-internal"  # noqa: S105 - a header name, not a secret
_warned_no_internal_secret = False


def _trusted_internal_caller(request: Request) -> bool:
    """True only when the request carries the shared service-to-service secret.

    X-User-Id is a bare claim: anyone who can reach this service can set it.
    It is honoured only next to a matching `x-studojo-internal` header, which
    the frontend's server-side client sends and a browser never has.
    """
    global _warned_no_internal_secret
    expected = settings.INTERNAL_API_SECRET
    if not expected:
        if not _warned_no_internal_secret:
            logger.warning(
                "[AUTH] INTERNAL_API_SECRET is not set; ignoring X-User-Id and "
                "falling back to the session cookie. Server-side callers will 401."
            )
            _warned_no_internal_secret = True
        return False
    supplied = request.headers.get(INTERNAL_SECRET_HEADER) or ""
    return hmac.compare_digest(supplied.encode(), expected.encode())


def require_internal_caller(request: Request) -> None:
    """Gate for cluster-internal worker routes (send cycle, mesa sweep).

    These used to rely on "only reachable in-cluster", but the studojo.com
    ingress forwards /api/v1/outreach/* here, so they were public. Callers
    (job-outreach-worker, the mesa CronJob) send the shared secret.
    """
    if not _trusted_internal_caller(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal endpoint",
        )


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Authenticate request via either:
    1. X-User-Id header from a server-side caller, trusted only alongside a
       matching x-studojo-internal secret (see _trusted_internal_caller)
    2. BetterAuth session cookie (browser-based clients)

    Plain `def` on purpose: every path here is blocking SQLAlchemy I/O, so
    FastAPI runs it in the threadpool instead of stalling the event loop.

    Raises:
        HTTPException 401 if no valid auth is found.
    """
    # ── Path 1: X-User-Id from a trusted server-side caller ─────────────────
    # Without the shared secret the header is ignored, not rejected, so a
    # browser that happens to send it still authenticates by cookie below.
    user_id = request.headers.get("X-User-Id")
    if user_id and _trusted_internal_caller(request):
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )
        return user

    # ── Path 2: BetterAuth session cookie (browser) ──────────────────────────
    token = None
    for name in COOKIE_NAMES:
        token = request.cookies.get(name)
        if token:
            token = unquote(token)
            # BetterAuth cookie format is "token.signature" — DB stores only the token part
            if "." in token:
                token = token.split(".")[0]
            break

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    session = (
        db.query(BetterAuthSession)
        .filter(BetterAuthSession.token == token)
        .first()
    )

    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )

    if session.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired",
        )

    user = db.query(User).filter(User.id == session.user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user


_ADMIN_KEY_TTL = 300  # seconds; BetterAuth rotates keys rarely
_admin_keys_cache: dict = {"at": 0.0, "keys": []}


def _betterauth_public_keys(db: Session) -> list:
    """(kid, PyJWK) for every non-expired BetterAuth signing key.

    The admin panel's token is a BetterAuth JWT from the main site, signed
    with a key stored in the shared `jwks` table (the admin panel's own server
    verifies against the same table). Cached briefly.
    """
    import jwt
    from sqlalchemy import text

    now = time.time()
    if now - _admin_keys_cache["at"] < _ADMIN_KEY_TTL and _admin_keys_cache["keys"]:
        return _admin_keys_cache["keys"]
    rows = db.execute(text(
        "SELECT id, public_key FROM jwks WHERE expires_at IS NULL OR expires_at > NOW()"
    )).fetchall()
    keys = []
    for kid, public_key in rows:
        try:
            jwk = json.loads(public_key)
            alg = jwk.get("alg") or ("EdDSA" if jwk.get("kty") == "OKP" else "RS256")
            keys.append((kid, jwt.PyJWK(jwk, algorithm=alg)))
        except Exception:
            logger.exception("[AUTH] unusable jwks row %s", kid)
    _admin_keys_cache.update(at=now, keys=keys)
    return keys


def _verified_admin_claims(db: Session, token: str) -> dict:
    """Claims of a BetterAuth JWT whose signature and expiry check out.

    This used to base64-decode the payload and trust it: anyone who knew an
    admin's user id could write their own token and get every admin route.
    """
    import jwt

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    keys = _betterauth_public_keys(db)
    kid = header.get("kid")
    candidates = [k for k_id, k in keys if kid and k_id == kid] or [k for _, k in keys]
    for key in candidates:
        try:
            return jwt.decode(
                token, key=key, algorithms=[key.algorithm_name],
                options={"require": ["exp", "sub"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired or invalid")
        except jwt.PyJWTError:
            continue
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def get_admin_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Verify admin access via a signed BetterAuth Bearer JWT.

    Signature and expiry are verified against the shared `jwks` table, then
    the user must hold the admin role. Plain `def`: DB work on the threadpool.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )

    claims = _verified_admin_claims(db, auth_header[7:])
    user_id = claims.get("sub")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    return user
