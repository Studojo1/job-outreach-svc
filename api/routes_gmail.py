"""Gmail OAuth Routes — Gmail Mailbox OAuth (separate from Login OAuth)."""

import logging
from typing import Optional

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
    verify_gmail_state,
)
from pydantic import BaseModel
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
):
    """Google redirects here (api.studojo.com). Hand the one-time code to the
    signed-in page, which finishes the connection.

    This used to store tokens for whatever user id `state` held, unsigned
    (audit N03). The session cookie is host-only on the main site and never
    reaches this host, so the connection is completed by POST /complete,
    which checks that the signed-in user is the one who started the flow.
    Otherwise an attacker could start a flow on their own account and get a
    victim to approve it, attaching the victim's inbox (read scope) to the
    attacker's account.
    """
    from urllib.parse import urlencode
    frontend_base = f"{settings.FRONTEND_URL}/connect/gmail"
    if not state or verify_gmail_state(state) is None:
        logger.warning("[GmailOAuth] Callback with missing/invalid/expired state")
        return RedirectResponse(url=f"{frontend_base}?status=error&message=invalid_state")
    return RedirectResponse(url=f"{frontend_base}?{urlencode({'gmail_code': code, 'gmail_state': state})}")


class GmailCompleteRequest(BaseModel):
    code: str
    state: str


@router.post("/complete")
async def gmail_oauth_complete(
    request: GmailCompleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Finish connecting Gmail for the signed-in user who started the flow."""
    state_user = verify_gmail_state(request.state)
    if state_user is None:
        raise HTTPException(status_code=400, detail="This Gmail link expired. Please connect again.")
    if state_user != str(current_user.id):
        logger.warning("[GmailOAuth] state user %s != signed-in user %s; refusing", state_user, current_user.id)
        raise HTTPException(status_code=403, detail="This Gmail connection was started from a different account.")

    user_id = state_user
    try:
        token_data = await exchange_gmail_code(request.code)
    except Exception as e:
        logger.error("[GmailOAuth] Code exchange failed for %s: %s", user_id, e)
        raise HTTPException(status_code=400, detail="Google did not accept this sign-in. Please connect again.") from e

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3599)
    granted_scopes = token_data.get("scope", "")

    # Both scopes are required: gmail.send to send, gmail.readonly to detect
    # replies and bounces. Users can untick either on Google's consent screen.
    missing = [s for s in ("gmail.send", "gmail.readonly") if s not in granted_scopes]
    if missing:
        logger.warning("[GmailOAuth] Missing scopes for %s: %s", user_id, missing)
        raise HTTPException(status_code=400, detail="missing_permissions")

    user_info = await get_google_user_info(access_token)
    email_address = user_info.get("email")
    if not email_address:
        raise HTTPException(status_code=400, detail="Google did not share an email address.")

    await store_user_tokens(
        db=db, user_id=user_id, email_address=email_address,
        access_token=access_token, refresh_token=refresh_token, expires_in=expires_in,
    )
    capture("gmail_connected", user_id, {"email_address": email_address, "provider": "gmail"})

    account = db.query(EmailAccount).filter_by(email_address=email_address, user_id=user_id).first()
    try:
        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, user_id, "gmail_connected", email_account_id=account.id if account else None)
    except Exception:
        logger.exception("[GmailOAuth] Funnel stage marking failed (non-fatal)")

    _resume_auth_paused_campaigns(db, user_id, email_address)
    logger.info("[GmailOAuth] Connected %s for %s", email_address, user_id)
    return {"status": "connected", "email_address": email_address,
            "email_account_id": account.id if account else None}


def _resume_auth_paused_campaigns(db: Session, user_id: str, email_address: str) -> None:
    """Reconnecting Gmail restarts what a dead grant stopped.

    The worker pauses a campaign (pause_reason='gmail_auth') instead of
    failing its queue when the mailbox loses access, keeping every unsent
    email. Before, reconnecting recovered nothing: 691 paid emails across 7
    paying campaigns stayed failed (audit P06). Only campaigns the system
    paused, on this same mailbox, are resumed; a user's own pause is theirs.
    """
    from database.models import Campaign, Candidate
    from services.email_campaign.campaign_service import transition_campaign
    from services.email_campaign.outcomes import PAUSE_REASON_GMAIL_AUTH
    try:
        paused = (
            db.query(Campaign)
            .join(Candidate, Candidate.id == Campaign.candidate_id)
            .join(EmailAccount, EmailAccount.id == Campaign.email_account_id)
            .filter(Candidate.user_id == user_id,
                    Campaign.status == "paused",
                    Campaign.pause_reason == PAUSE_REASON_GMAIL_AUTH,
                    EmailAccount.email_address == email_address)
            .all()
        )
        for campaign in paused:
            transition_campaign(db, campaign.id, "running", actor="system")
            logger.info("[GmailOAuth] Resumed campaign %s after %s reconnected", campaign.id, email_address)
    except Exception:
        db.rollback()
        logger.exception("[GmailOAuth] Could not resume auth-paused campaigns for %s", user_id)


@router.get("/account")
async def get_gmail_account(
    email_account_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current user's connected Gmail account ID, email, and token health.

    token_valid is True/False when Google answered, and None when the check
    itself failed (timeout, DNS). It used to report False for any network
    blip, which forced the "your emails cannot be sent" banner on healthy
    mailboxes (audit P48). email_account_id, when given, picks that account
    instead of an arbitrary one.
    """
    from services.connections import gmail_connected_filter
    q = db.query(EmailAccount).filter_by(user_id=str(current_user.id), provider="gmail").filter(gmail_connected_filter())
    account = (q.filter_by(id=email_account_id).first() if email_account_id else None) or q.first()

    if not account:
        raise HTTPException(status_code=404, detail="No Gmail account connected")

    # Verify the refresh token is still valid
    token_valid: Optional[bool] = None
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
        if resp.status_code == 200:
            token_valid = True
        elif resp.status_code in (400, 401):
            token_valid = False  # invalid_grant / revoked: really dead
    except Exception:
        logger.warning("[GmailOAuth] Token validation request failed for account %s", account.id)

    return {
        "email_account_id": account.id,
        "email_address": account.email_address,
        "token_valid": token_valid,
    }
