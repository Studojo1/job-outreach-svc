"""Gmail OAuth Routes — Gmail Mailbox OAuth (separate from Login OAuth)."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from job_outreach_tool.database.session import get_db
from job_outreach_tool.database.models import User
from job_outreach_tool.core.config import settings
from job_outreach_tool.services.authentication.google_oauth import (
    generate_gmail_auth_url,
    exchange_gmail_code,
    get_google_user_info,
)
from job_outreach_tool.services.authentication.token_manager import store_user_tokens

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gmail/oauth", tags=["Gmail OAuth"])


@router.get("/connect")
def gmail_oauth_connect(token: str = Query(None)):
    """Redirect to Google OAuth to grant Gmail send/read permissions.

    The frontend passes the JWT as a query param since browser redirects
    (window.location.href) cannot send Authorization headers.
    """
    if not token:
        logger.error("[GmailOAuth] /connect called without token query param")
        raise HTTPException(status_code=401, detail="Token required. Pass ?token=<jwt>")

    # Verify the token manually since we can't use Depends(get_current_user)
    from job_outreach_tool.services.authentication.jwt_service import verify_token
    payload = verify_token(token)
    if not payload:
        logger.error("[GmailOAuth] Invalid or expired JWT token")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    logger.info(f"[GmailOAuth] OAuth flow started for user_id={user_id}")
    url = generate_gmail_auth_url(str(user_id))
    logger.info("[GmailOAuth] Redirecting to Google")
    return RedirectResponse(url=url)


@router.get("/callback")
async def gmail_oauth_callback(
    code: str,
    state: str = "",
    db: Session = Depends(get_db),
):
    """Gmail OAuth callback — stores Gmail tokens for the user."""
    logger.info(f"[GmailOAuth] Callback received, state={state}")

    if not state:
        logger.error("[GmailOAuth] Callback missing state parameter")
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/connect/gmail?status=error&message=missing_state"
        )

    user_id = state
    try:
        token_data = await exchange_gmail_code(code)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3599)

        user_info = await get_google_user_info(access_token)
        email_address = user_info.get("email")

        if not email_address:
            raise ValueError("Email address missing from Google OAuth profile.")

        await store_user_tokens(
            db=db,
            user_id=user_id,
            email_address=email_address,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )

        logger.info(f"[GmailOAuth] Gmail account connected successfully for user_id={user_id}, email={email_address}")

        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/connect/gmail?status=success"
        )
    except Exception as e:
        logger.error(f"[GmailOAuth] Callback failed: {e}", exc_info=True)
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/connect/gmail?status=error&message={str(e)}"
        )