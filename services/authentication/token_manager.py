import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from job_outreach_tool.database.models import EmailAccount
from job_outreach_tool.services.authentication.google_oauth import refresh_gmail_access_token

logger = logging.getLogger(__name__)

async def store_user_tokens(db: Session, user_id: str, email_address: str, access_token: str, refresh_token: Optional[str], expires_in: int):
    """Stores or updates the user's Gmail tokens in the database."""
    try:
        # Give a small buffer (e.g., 60 seconds) so we trigger refresh slightly before true expiry
        token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)
        
        # Check if account already exists
        account = db.query(EmailAccount).filter(
            EmailAccount.user_id == str(user_id),
            EmailAccount.provider == "gmail"
        ).first()

        if account:
            account.access_token = access_token
            # Only update refresh token if a new one is provided (Google doesn't always send a new one on refresh)
            if refresh_token:
                account.refresh_token = refresh_token
            account.token_expiry = token_expiry
            account.email_address = email_address
            logger.info(f"Updated existing Gmail tokens for user_id: {user_id}")
        else:
            account = EmailAccount(
                user_id=str(user_id),
                email_address=email_address,
                provider="gmail",
                access_token=access_token,
                refresh_token=refresh_token,
                token_expiry=token_expiry
            )
            db.add(account)
            logger.info(f"Stored new Gmail tokens for user_id: {user_id}")
            
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error storing user tokens for user_id {user_id}: {str(e)}")
        raise

def get_user_token(db: Session, user_id: str) -> Optional[EmailAccount]:
    """Retrieves the user's token record without enforcing expiry evaluation."""
    account = db.query(EmailAccount).filter(
        EmailAccount.user_id == str(user_id),
        EmailAccount.provider == "gmail"
    ).first()
    
    if not account:
        logger.info(f"No Gmail token found for user_id: {user_id}")
        return None
        
    return account

async def refresh_access_token(db: Session, user_id: str) -> Optional[EmailAccount]:
    """Forces a refresh of the access token if a refresh token is present."""
    account = get_user_token(db, user_id)
    if not account:
        logger.error(f"Cannot refresh token: No account found for user_id {user_id}")
        return None
    
    if not account.refresh_token:
        logger.error(f"Cannot refresh token for user_id {user_id}: No refresh_token stored.")
        return None
        
    try:
        new_token_data = await refresh_gmail_access_token(account.refresh_token)
        
        # Update db
        new_access_token = new_token_data.get("access_token")
        new_expires_in = new_token_data.get("expires_in", 3599)
        new_refresh_token = new_token_data.get("refresh_token") # May or may not be present
        
        await store_user_tokens(
            db=db,
            user_id=user_id,
            email_address=account.email_address,
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=new_expires_in
        )
        
        logger.info(f"Successfully refreshed and stored new token for user_id: {user_id}")
        return get_user_token(db, user_id)
        
    except Exception as e:
        logger.error(f"Failed to complete token refresh process for user_id {user_id}: {str(e)}")
        raise
