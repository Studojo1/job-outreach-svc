"""Connected accounts and data export (Privacy Policy v2.0 / Terms v2.0).

GET  /account/connections   which of Gmail and LinkedIn are connected
POST /gmail/disconnect      revoke Google's grant, delete the tokens, pause running campaigns
POST /linkedin/disconnect   delete stored LinkedIn cookies, pause running LinkedIn campaigns
GET  /account/export        download my data (JSON attachment)

All four act only on the signed-in user (get_current_user, same as the Gmail routes).
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from database.models import User
from database.session import get_db
from services.connections import connection_status, disconnect_gmail, disconnect_linkedin
from services.data_export import export_user_data

router = APIRouter(tags=["Connections"])


@router.get("/account/connections")
def get_connections(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return connection_status(db, str(current_user.id))


@router.post("/gmail/disconnect")
def gmail_disconnect(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return disconnect_gmail(db, str(current_user.id))


@router.post("/linkedin/disconnect")
def linkedin_disconnect(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return disconnect_linkedin(db, str(current_user.id))


@router.get("/account/export")
def export_my_data(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = export_user_data(db, str(current_user.id))
    filename = f"studojo-data-{datetime.utcnow().date().isoformat()}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
