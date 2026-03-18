"""Authentication Routes — BetterAuth session validation only."""

from fastapi import APIRouter, Depends, Request

from database.models import User
from api.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])


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
