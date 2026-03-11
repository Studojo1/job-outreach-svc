import logging
from typing import Optional
from sqlalchemy.orm import Session
from datetime import datetime

from services.authentication.google_oauth import generate_gmail_auth_url, exchange_gmail_code, get_google_user_info
from services.authentication.token_manager import store_user_tokens, get_user_token, refresh_access_token

logger = logging.getLogger(__name__)

def connect_google_account(user_id: str) -> str:
    """Returns the Gmail OAuth authorization URL to redirect the user to."""
    logger.info(f"Initiating Gmail connection for user {user_id}")
    return generate_gmail_auth_url(user_id)

async def process_oauth_callback(db: Session, code: str, user_id: str) -> bool:
    """Processes the Gmail OAuth callback, fetches tokens and user info, and stores them in the DB."""
    try:
        token_data = await exchange_gmail_code(code)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3599)

        user_info = await get_google_user_info(access_token)
        email_address = user_info.get("email")

        if not email_address:
            logger.error("Google user info did not contain an email address.")
            raise ValueError("Email address missing from Google OAuth profile.")

        await store_user_tokens(
            db=db,
            user_id=user_id,
            email_address=email_address,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in
        )
        logger.info(f"Successfully processed OAuth callback and saved tokens for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error processing oauth callback for user {user_id}: {str(e)}")
        raise

async def get_gmail_token(db: Session, user_id: str) -> Optional[str]:
    """Gets a valid access token for the user, refreshing automatically if expired."""
    account = get_user_token(db, user_id)
    if not account:
        logger.warning(f"No connected Gmail account found for user {user_id}")
        return None

    if account.token_expiry and account.token_expiry <= datetime.utcnow():
        logger.info(f"Token expired for user {user_id}, attempting automatic refresh.")
        account = await refresh_access_token(db, user_id)
        if not account:
            return None

    return account.access_token