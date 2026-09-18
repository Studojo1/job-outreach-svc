from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class GoogleOAuthResponse(BaseModel):
    access_token: str
    expires_in: int
    refresh_token: Optional[str] = None
    scope: str
    token_type: str
    
class GmailToken(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_expiry: Optional[datetime] = None
