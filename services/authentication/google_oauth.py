"""Google OAuth utilities — Gmail OAuth client only.

Login OAuth is handled by BetterAuth on the main Studojo frontend.
"""

import httpx
import logging
from typing import Dict, Any
from urllib.parse import urlencode

from core.config import settings

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a public OAuth endpoint URL, not a secret

# --- Gmail OAuth client ---
GMAIL_CLIENT_ID = settings.GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET = settings.GMAIL_CLIENT_SECRET
GMAIL_REDIRECT_URI = settings.GMAIL_REDIRECT_URI

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]


STATE_TTL_SECONDS = 20 * 60
_STATE_PURPOSE = "gmail_oauth_state"


def _state_key() -> bytes:
    # Derived from the Gmail client secret: always configured, never sent to
    # a browser, and specific to this OAuth client.
    import hashlib
    return hashlib.sha256(f"{_STATE_PURPOSE}:{GMAIL_CLIENT_SECRET}".encode()).digest()


def sign_gmail_state(user_id: str) -> str:
    """OAuth `state` naming the user who started the flow, signed and short-lived.

    It used to be the bare user id, so anyone could complete Google's consent
    with their own Gmail and put someone else's id in `state` (audit N03).
    """
    import secrets
    import time
    import jwt
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "purpose": _STATE_PURPOSE, "iat": now,
         "exp": now + STATE_TTL_SECONDS, "nonce": secrets.token_urlsafe(8)},
        _state_key(), algorithm="HS256",
    )


def verify_gmail_state(state: str):
    """The user id a state was signed for, or None if forged, expired or garbled."""
    import jwt
    try:
        claims = jwt.decode(state, _state_key(), algorithms=["HS256"],
                            options={"require": ["sub", "exp", "purpose"]})
    except jwt.PyJWTError:
        return None
    return claims["sub"] if claims.get("purpose") == _STATE_PURPOSE else None


def generate_gmail_auth_url(user_id: str) -> str:
    """Generates the Gmail OAuth consent URL using the Gmail OAuth client."""
    params = urlencode({
        "client_id": GMAIL_CLIENT_ID,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": sign_gmail_state(user_id),
    })
    logger.info(f"Generated Gmail OAuth redirect URL for user_id: {user_id}")
    return f"{GOOGLE_AUTH_URL}?{params}"


async def exchange_code_for_tokens(code: str, client_id: str, client_secret: str, redirect_uri: str) -> Dict[str, Any]:
    """Exchanges the authorization code for tokens using the specified client credentials."""
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    logger.info("Exchanging authorization code for Google tokens.")

    async with httpx.AsyncClient() as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=data)

        if response.status_code != 200:
            logger.error(f"Failed to exchange token. Status: {response.status_code}, Response: {response.text}")
            raise Exception("Failed to exchange authorization code for tokens.")

        token_data = response.json()
        logger.info("Successfully exchanged authorization code for Google tokens.")
        return token_data


async def exchange_gmail_code(code: str) -> Dict[str, Any]:
    """Exchange code using the Gmail OAuth client."""
    return await exchange_code_for_tokens(code, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REDIRECT_URI)


async def refresh_gmail_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refreshes an expired Gmail access token using the Gmail OAuth client."""
    data = {
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    logger.info("Attempting to refresh Gmail access token.")

    async with httpx.AsyncClient() as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=data)

        if response.status_code != 200:
            logger.error(f"Failed to refresh token. Status: {response.status_code}, Response: {response.text}")
            raise Exception("Failed to refresh Google access token.")

        token_data = response.json()
        logger.info("Successfully refreshed Gmail access token.")
        return token_data


async def get_google_user_info(access_token: str) -> Dict[str, Any]:
    """Fetches user profile information using the access token."""
    url = "https://www.googleapis.com/oauth2/v2/userinfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            logger.error("Failed to fetch Google user info.")
            raise Exception("Failed to fetch user info from Google.")
        return response.json()