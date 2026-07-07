"""Bob (Mesa) — placement intelligence workspace API.

Shared-workspace auth: one access code per deployment (BOB_ACCESS_CODE),
sent as X-Bob-Key on every request. No per-user accounts in v1.
"""

import io
import json
import logging

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.config import settings
from database.session import get_db
from services.bob.schema import ensure_schema
from services.bob.agent import start_run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bob", tags=["Bob"])


# ── Auth + schema bootstrap ────────────────────────────────────────────────────

def _guard(x_bob_key: str = Header(default="")) -> None:
    if not settings.BOB_ACCESS_CODE:
        raise HTTPException(status_code=503, detail="Bob is not configured on this environment")
    if x_bob_key != settings.BOB_ACCESS_CODE:
        raise HTTPException(status_code=401, detail="Invalid access code")
    ensure_schema()


class VerifyRequest(BaseModel):
    code: str


@router.post("/auth/verify")
async def verify_code(req: VerifyRequest):
    if not settings.BOB_ACCESS_CODE:
        raise HTTPException(status_code=503, detail="Bob is not configured on this environment")
    if req.code != settings.BOB_ACCESS_CODE:
        raise HTTPException(status_code=401, detail="Invalid access code")
    ensure_schema()
    return {"ok": True}


# ── Chats ──────────────────────────────────────────────────────────────────────

@router.get("/chats", dependencies=[Depends(_guard)])
async def list_chats(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT id, title, created_at, updated_at FROM bob_chats ORDER BY updated_at DESC LIMIT 200")
    ).fetchall()
    return {"chats": [
        {"id": r[0], "title": r[1], "created_at": str(r[2]), "updated_at": str(r[3])} for r in rows
    ]}


@router.post("/chats", dependencies=[Depends(_guard)])
async def create_chat(db: Session = Depends(get_db)):
    row = db.execute(text("INSERT INTO bob_chats (title) VALUES ('New chat') RETURNING id")).fetchone()
    db.commit()
    return {"id": row[0], "title": "New chat"}


@router.delete("/chats/{chat_id}", dependencies=[Depends(_guard)])
async def delete_chat(chat_id: int, db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM bob_chats WHERE id = :c"), {"c": chat_id})
    db.commit()
    return {"ok": True}


@router.get("/chats/{chat_id}", dependencies=[Depends(_guard)])
async def get_chat(chat_id: int, db: Session = Depends(get_db)):
    chat = db.execute(
        text("SELECT id, title FROM bob_chats WHERE id = :c"), {"c": chat_id}
    ).fetchone()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    messages = db.execute(
        text("SELECT id, role, content, created_at FROM bob_messages WHERE chat_id = :c ORDER BY id"),
        {"c": chat_id},
    ).fetchall()
    run = db.execute(
        text("SELECT id, status, events, counters, credits_used, answer FROM bob_runs "
             "WHERE chat_id = :c ORDER BY id DESC LIMIT 1"),
        {"c": chat_id},
    ).fetchone()

    return {
        "id": chat[0],
        "title": chat[1],
        "messages": [
            {"id": m[0], "role": m[1], "content": m[2], "created_at": str(m[3])} for m in messages
        ],
        "latest_run": _run_payload(run) if run else None,
        "tables": _tables_payload(db, chat_id),
    }


# ── File attachments ───────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/chats/{chat_id}/files", dependencies=[Depends(_guard)])
async def upload_chat_file(chat_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    from services.bob.files import extract_text, FileExtractionError

    chat = db.execute(text("SELECT id FROM bob_chats WHERE id = :c"), {"c": chat_id}).fetchone()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    try:
        extracted = extract_text(file.filename or "upload", data)
    except FileExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e))

    row = db.execute(
        text("INSERT INTO bob_files (chat_id, filename, mime, text_content) "
             "VALUES (:c, :f, :m, :t) RETURNING id"),
        {"c": chat_id, "f": (file.filename or "upload")[:200],
         "m": (file.content_type or "")[:100], "t": extracted},
    ).fetchone()
    db.commit()
    return {"file_id": row[0], "filename": file.filename, "chars_extracted": len(extracted)}


# ── Messages / runs ────────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    content: str


@router.post("/chats/{chat_id}/messages", dependencies=[Depends(_guard)])
async def send_message(chat_id: int, req: SendMessageRequest, db: Session = Depends(get_db)):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty message")
    if len(content) > 8000:
        content = content[:8000]

    chat = db.execute(text("SELECT id, title FROM bob_chats WHERE id = :c"), {"c": chat_id}).fetchone()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # A run whose thread died (pod restart, crash) stays 'running' forever —
    # treat anything silent for 15+ minutes as dead so the chat never wedges.
    running = db.execute(
        text("SELECT id FROM bob_runs WHERE chat_id = :c AND status = 'running' "
             "AND updated_at > now() - interval '15 minutes'"),
        {"c": chat_id},
    ).fetchone()
    if running:
        raise HTTPException(status_code=409, detail="Bob is still working on this chat")
    db.execute(
        text("UPDATE bob_runs SET status = 'error', error = 'stale run reaped', updated_at = now() "
             "WHERE chat_id = :c AND status = 'running'"),
        {"c": chat_id},
    )

    db.execute(
        text("INSERT INTO bob_messages (chat_id, role, content) VALUES (:c, 'user', :m)"),
        {"c": chat_id, "m": content},
    )
    if chat[1] == "New chat":
        db.execute(
            text("UPDATE bob_chats SET title = :t, updated_at = now() WHERE id = :c"),
            {"t": content[:70], "c": chat_id},
        )
    else:
        db.execute(text("UPDATE bob_chats SET updated_at = now() WHERE id = :c"), {"c": chat_id})
    db.commit()

    run_id = start_run(chat_id)
    return {"run_id": run_id}


@router.get("/runs/{run_id}", dependencies=[Depends(_guard)])
async def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.execute(
        text("SELECT id, status, events, counters, credits_used, answer, chat_id FROM bob_runs WHERE id = :r"),
        {"r": run_id},
    ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    payload = _run_payload(run)
    payload["tables"] = _tables_payload(db, run[6])
    return payload


# ── Rows: status updates (team coordination) ──────────────────────────────────

class RowStatusRequest(BaseModel):
    status: str


@router.patch("/rows/{row_id}", dependencies=[Depends(_guard)])
async def update_row_status(row_id: int, req: RowStatusRequest, db: Session = Depends(get_db)):
    if req.status not in ("new", "contacted", "replied", "meeting", "dead"):
        raise HTTPException(status_code=400, detail="Invalid status")
    db.execute(
        text("UPDATE bob_rows SET status = :s, updated_at = now() WHERE id = :r"),
        {"s": req.status, "r": row_id},
    )
    db.commit()
    return {"ok": True}


# ── Export ─────────────────────────────────────────────────────────────────────

@router.get("/tables/{table_id}/export", dependencies=[Depends(_guard)])
async def export_table(table_id: int, db: Session = Depends(get_db)):
    tbl = db.execute(
        text("SELECT name, columns FROM bob_tables WHERE id = :t"), {"t": table_id}
    ).fetchone()
    if not tbl:
        raise HTTPException(status_code=404, detail="Table not found")
    rows = db.execute(
        text("SELECT cells, status FROM bob_rows WHERE table_id = :t ORDER BY position, id"),
        {"t": table_id},
    ).fetchall()

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = (tbl[0] or "Results")[:31]
    columns = tbl[1] or []
    headers = [c.get("label") or c.get("key") for c in columns] + ["Status"]
    ws.append(headers)
    for cells, status in rows:
        cells = cells or {}
        ws.append([_cell_str(cells.get(c.get("key"))) for c in columns] + [status])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = (tbl[0] or "results").replace('"', "")[:60]
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}.xlsx"'},
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _run_payload(run) -> dict:
    return {
        "id": run[0],
        "status": run[1],
        "events": run[2] or [],
        "counters": run[3] or {},
        "credits_used": run[4] or 0,
        "answer": run[5] or "",
    }


def _tables_payload(db: Session, chat_id: int) -> list[dict]:
    tables = db.execute(
        text("SELECT id, name, columns FROM bob_tables WHERE chat_id = :c ORDER BY id"),
        {"c": chat_id},
    ).fetchall()
    out = []
    for tid, name, columns in tables:
        rows = db.execute(
            text("SELECT id, cells, status FROM bob_rows WHERE table_id = :t ORDER BY position, id LIMIT 500"),
            {"t": tid},
        ).fetchall()
        out.append({
            "id": tid,
            "name": name,
            "columns": columns or [],
            "rows": [{"id": r[0], "cells": r[1] or {}, "status": r[2]} for r in rows],
        })
    return out
