"""API Dependencies — Shared FastAPI dependencies for route handlers."""

import base64
import json
import time
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database.session import get_db
from database.models import BetterAuthSession, User


COOKIE_NAMES = [
    "__Secure-better-auth.session_token",
    "better-auth.session_token",
]


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Authenticate request via either:
    1. X-User-Id header injected by the control-plane after JWT validation (extension / API clients)
    2. BetterAuth session cookie (browser-based clients)

    Raises:
        HTTPException 401 if no valid auth is found.
    """
    # ── Path 1: X-User-Id from control-plane (JWT already validated upstream) ──
    user_id = request.headers.get("X-User-Id")
    if user_id:
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


async def get_admin_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Verify admin access via Bearer JWT token.

    The admin panel sends BetterAuth JWTs as Bearer tokens.
    We decode the payload to extract user_id, look up the user,
    and verify they have the admin role.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )

    token = auth_header[7:]

    # Decode JWT payload (base64) to get sub (user_id)
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        user_id = payload.get("sub")
        exp = payload.get("exp", 0)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if not user_id or exp < time.time():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired or invalid",
        )

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
