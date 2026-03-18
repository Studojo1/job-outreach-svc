"""API Dependencies — Shared FastAPI dependencies for route handlers."""

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
    """Extract BetterAuth session token from cookie, look up in shared DB.

    Raises:
        HTTPException 401 if cookie is missing, session expired, or user not found.
    """
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
