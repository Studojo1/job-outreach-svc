"""Account routes: self-serve deletion (B2C open item NEW-04)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from database.models import User
from database.session import get_db
from services.account_deletion import UnclassifiedUserTables, delete_account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["Account"])


class DeleteAccountRequest(BaseModel):
    confirm: str


@router.post("/delete")
def delete_my_account(
    request: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete the signed-in user's account. The body must say {"confirm": "DELETE"}."""
    if request.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='Type DELETE to confirm.')
    try:
        report = delete_account(db, str(current_user.id))
    except UnclassifiedUserTables as e:
        logger.error("[ACCOUNT_DELETE] refused for %s: unclassified tables %s", current_user.id, e)
        raise HTTPException(
            status_code=503,
            detail="We could not delete your account automatically. Please raise a ticket and we will do it by hand.",
        ) from None
    return {"status": "deleted", "revoked_gmail": report["revoked_gmail"]}
