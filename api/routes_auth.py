"""Authentication Routes — Google Sign-In (Login OAuth only)."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from job_outreach_tool.database.session import get_db
from job_outreach_tool.database.models import User
from job_outreach_tool.core.config import settings
from job_outreach_tool.services.authentication.jwt_service import create_access_token
from job_outreach_tool.services.authentication.google_oauth import (
    generate_login_auth_url,
    exchange_login_code,
    get_google_user_info,
)
from job_outreach_tool.api.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/google/login")
def auth_google_login():
    """Redirect to Google Sign-In consent page (Login OAuth client)."""
    url = generate_login_auth_url()
    return RedirectResponse(url=url)


@router.get("/google/callback")
async def auth_google_callback(
    code: str,
    state: str = "login",
    db: Session = Depends(get_db),
):
    """Login OAuth callback — creates/finds user and returns JWT."""
    try:
        token_data = await exchange_login_code(code)
        access_token = token_data.get("access_token")

        user_info = await get_google_user_info(access_token)
        email = user_info.get("email")
        google_sub = user_info.get("id")
        name = user_info.get("name", "")
        avatar = user_info.get("picture", "")

        if not email:
            raise HTTPException(status_code=400, detail="Email not provided by Google")

        # Find or create user
        user = db.query(User).filter_by(email=email).first()
        if not user:
            user = User(
                email=email,
                google_sub=google_sub,
                name=name,
                avatar_url=avatar,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            if google_sub and not user.google_sub:
                user.google_sub = google_sub
            if name:
                user.name = name
            if avatar:
                user.avatar_url = avatar
            db.commit()

        jwt_token = create_access_token(user.id, user.email)

        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/login?token={jwt_token}"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/me")
def auth_me(current_user: User = Depends(get_current_user)):
    """Return current authenticated user info."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "avatar_url": current_user.avatar_url,
    }


@router.post("/logout")
def auth_logout():
    """Logout (client-side: discard the JWT token)."""
    return {"status": "ok", "message": "Token discarded on client side"}