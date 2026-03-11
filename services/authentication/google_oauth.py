"""Google OAuth utilities — separate clients for Login vs Gmail."""

import httpx
import logging
from typing import Dict, Any
from urllib.parse import urlencode

from core.config import settings

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# --- Login OAuth client ---
LOGIN_CLIENT_ID = settings.GOOGLE_LOGIN_CLIENT_ID
LOGIN_CLIENT_SECRET = settings.GOOGLE_LOGIN_CLIENT_SECRET
LOGIN_REDIRECT_URI = settings.GOOGLE_LOGIN_REDIRECT_URI

LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# --- Gmail OAuth client ---
GMAIL_CLIENT_ID = settings.GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET = settings.GMAIL_CLIENT_SECRET
GMAIL_REDIRECT_URI = settings.GMAIL_REDIRECT_URI

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]


def generate_login_auth_url() -> str:
    """Generates the Google Sign-In consent URL using the Login OAuth client."""
    params = urlencode({
        "client_id": LOGIN_CLIENT_ID,
        "redirect_uri": LOGIN_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": "login",
    })
    return f"{GOOGLE_AUTH_URL}?{params}"


def generate_gmail_auth_url(user_id: str) -> str:
    """Generates the Gmail OAuth consent URL using the Gmail OAuth client."""
    params = urlencode({
        "client_id": GMAIL_CLIENT_ID,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": user_id,
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


async def exchange_login_code(code: str) -> Dict[str, Any]:
    """Exchange code using the Login OAuth client."""
    return await exchange_code_for_tokens(code, LOGIN_CLIENT_ID, LOGIN_CLIENT_SECRET, LOGIN_REDIRECT_URI)


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