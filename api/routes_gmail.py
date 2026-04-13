"""Gmail OAuth Routes — Gmail Mailbox OAuth (separate from Login OAuth)."""

import logging
from urllib.parse import quote

import requests as http_requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from database.session import get_db
from database.models import User, EmailAccount
from core.config import settings
from services.authentication.google_oauth import (
    generate_gmail_auth_url,
    exchange_gmail_code,
    get_google_user_info,
)
from services.authentication.token_manager import store_user_tokens
from api.dependencies import get_current_user
from core.analytics import capture

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gmail/oauth", tags=["Gmail OAuth"])


@router.get("/connect")
def gmail_oauth_connect(current_user: User = Depends(get_current_user)):
    """Redirect to Google OAuth to grant Gmail send/read permissions."""
    logger.info(f"[GmailOAuth] OAuth flow started for user_id={current_user.id}")
    url = generate_gmail_auth_url(str(current_user.id))
    logger.info("[GmailOAuth] Redirecting to Google, redirect_uri=%s", settings.GMAIL_REDIRECT_URI)
    return RedirectResponse(url=url)


@router.get("/connect-url")
def gmail_oauth_connect_url(current_user: User = Depends(get_current_user)):
    """Return the Google OAuth URL as JSON (for authenticated frontend calls)."""
    logger.info(f"[GmailOAuth] OAuth URL requested for user_id={current_user.id}")
    url = generate_gmail_auth_url(str(current_user.id))
    return {"url": url}


@router.get("/callback")
async def gmail_oauth_callback(
    code: str,
    state: str = "",
    db: Session = Depends(get_db),
):
    """Gmail OAuth callback — stores Gmail tokens for the user."""
    frontend_base = f"{settings.FRONTEND_URL}/connect/gmail"
    logger.info(f"[GmailOAuth] Callback received, state={state}, code_length={len(code)}")
    logger.info(f"[GmailOAuth] Will redirect to frontend_base={frontend_base}")

    if not state:
        logger.error("[GmailOAuth] Callback missing state parameter")
        return RedirectResponse(
            url=f"{frontend_base}?status=error&message=missing_state"
        )

    user_id = state
    try:
        logger.info(f"[GmailOAuth] Step 1: Exchanging code for tokens (user_id={user_id})")
        token_data = await exchange_gmail_code(code)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3599)
        granted_scopes = token_data.get("scope", "")
        logger.info(f"[GmailOAuth] Step 1 OK: got access_token={bool(access_token)}, refresh_token={bool(refresh_token)}, expires_in={expires_in}, scopes={granted_scopes}")

        if "gmail.send" not in granted_scopes:
            logger.warning(f"[GmailOAuth] gmail.send scope NOT granted for user_id={user_id}. Granted: {granted_scopes}")
            return RedirectResponse(
                url=f"{frontend_base}?status=error&message=missing_send_permission"
            )

        logger.info(f"[GmailOAuth] Step 2: Fetching Google user info")
        user_info = await get_google_user_info(access_token)
        email_address = user_info.get("email")
        logger.info(f"[GmailOAuth] Step 2 OK: email={email_address}")

        if not email_address:
            raise ValueError("Email address missing from Google OAuth profile.")

        logger.info(f"[GmailOAuth] Step 3: Storing tokens for user_id={user_id}")
        await store_user_tokens(
            db=db,
            user_id=user_id,
            email_address=email_address,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )
        logger.info(f"[GmailOAuth] Step 3 OK: tokens stored")
        capture("gmail_connected", user_id, {
            "email_address": email_address,
            "provider": "gmail",
        })

        redirect_url = f"{frontend_base}?status=success"
        logger.info(f"[GmailOAuth] SUCCESS — redirecting to {redirect_url}")
        return RedirectResponse(url=redirect_url)

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error(f"[GmailOAuth] Callback FAILED for user_id={user_id}: {error_msg}", exc_info=True)
        encoded_msg = quote(error_msg, safe="")
        redirect_url = f"{frontend_base}?status=error&message={encoded_msg}"
        logger.info(f"[GmailOAuth] ERROR — redirecting to {redirect_url}")
        return RedirectResponse(url=redirect_url)


@router.get("/account")
async def get_gmail_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current user's connected Gmail account ID, email, and token health."""
    account = db.query(EmailAccount).filter_by(
        user_id=str(current_user.id), provider="gmail"
    ).first()

    if not account:
        raise HTTPException(status_code=404, detail="No Gmail account connected")

    # Verify the refresh token is still valid
    token_valid = False
    try:
        resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.GMAIL_CLIENT_ID,
                "client_secret": settings.GMAIL_CLIENT_SECRET,
                "refresh_token": account.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=5,
        )
        token_valid = resp.status_code == 200
    except Exception:
        logger.warning("[GmailOAuth] Token validation request failed for account %s", account.id)

    return {
        "email_account_id": account.id,
        "email_address": account.email_address,
        "token_valid": token_valid,
    }
