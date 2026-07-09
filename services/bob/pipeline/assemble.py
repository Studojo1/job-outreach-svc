"""Assembly stage: contacted opportunities -> table rows.

Selection is deterministic (fit desc, capped at the remaining target).
Leftovers KEEP status='contacted': they are the visible backlog the next
run ships first, never a silent loss. Cells reuse the same sanitize rail
the agent used, and the table's column set is merged so every produced
field renders.
"""

import json
import logging

from sqlalchemy import text

from services.bob.pipeline import state
from services.bob.textrails import sanitize_cells

logger = logging.getLogger(__name__)

# Columns assembly can produce, in display order (merged into the table).
_COLUMNS = [
    ("company", "company"), ("role", "role"), ("city", "city"),
    ("stipend", "stipend"), ("posted", "posted"), ("source", "source"),
    ("fit_score", "fit_score"), ("tier", "tier"),
    ("contact_name", "contact_name"), ("contact_title", "contact_title"),
    ("contact_email", "contact_email"), ("contact_linkedin_url", "contact_linkedin_url"),
    ("website", "website"), ("what_they_do", "what_they_do"),
    ("why_now", "why_now"), ("hiring_evidence", "hiring_evidence"),
    ("evidence_url", "evidence_url"), ("candidate", "candidate"),
]

_SOURCE_LABEL = {
    "ctx_li_posts": "LinkedIn post", "ctx_x": "X post", "ctx_boards": "Job board",
    "linkedin_jobs": "LinkedIn job", "unstop": "Unstop",
}


def build_cells(opp: dict, candidate: str = "") -> dict:
    source = _SOURCE_LABEL.get(opp.get("source") or "", opp.get("source") or "")
    if opp.get("author_affiliation") == "aggregator":
        source += " (via aggregator post)"
    posted = opp.get("posted") or ""
    cells = {
        "company": opp["company"],
        "role": opp.get("role") or "",
        "city": opp.get("location") or "",
        "stipend": opp.get("stipend") or "not stated",
        "posted": posted,
        "source": source,
        "fit_score": str(opp.get("fit_score") or ""),
        "tier": opp.get("contact_tier") or "",
        "contact_name": opp.get("contact_name") or "",
        "contact_title": opp.get("contact_title") or "",
        "contact_email": opp.get("contact_email") or "",
        "contact_linkedin_url": opp.get("contact_profile_url") or "",
        "website": opp.get("website") or "",
        "what_they_do": opp.get("what_they_do") or "",
        "why_now": f"Live {source} evidence" + (f", posted {posted}" if posted else ""),
        "hiring_evidence": opp.get("evidence_quote") or "",
        "evidence_url": opp.get("evidence_url") or "",
    }
    if candidate:
        cells["candidate"] = candidate
    clean, _removed = sanitize_cells(cells)
    return clean


def _merge_columns(db, table_id: int) -> None:
    existing = db.execute(text("SELECT columns FROM bob_tables WHERE id = :t"),
                          {"t": table_id}).scalar() or []
    keys = {c.get("key") for c in existing}
    add = [{"key": k, "label": lbl} for k, lbl in _COLUMNS if k not in keys]
    if add:
        db.execute(text("UPDATE bob_tables SET columns = CAST(:c AS jsonb), updated_at = now() WHERE id = :t"),
                   {"c": json.dumps(list(existing) + add), "t": table_id})
        db.commit()


def run(db, run_id: int, chat_id: int, params: dict, need: int) -> dict:
    """Write up to `need` best rows. Backlog stays queryable, not forgotten."""
    if need <= 0:
        return {"written": 0, "note": "target already met"}
    table_id = int(params["table_id"])
    pool = state.opportunities(db, chat_id=chat_id, status="contacted")
    pool.sort(key=lambda o: -(o.get("fit_score") or 0))
    table_norms = state.table_companies(db, table_id)
    _merge_columns(db, table_id)
    pos = db.execute(text("SELECT coalesce(max(position),0) FROM bob_rows WHERE table_id = :t"),
                     {"t": table_id}).scalar()

    written = 0
    for o in pool:
        if written >= need:
            break
        if o["company_norm"] in table_norms:   # user may have edited mid-run
            state.reject(db, o["id"], "assemble", "company landed in table while pipeline ran")
            continue
        cells = build_cells(o, candidate=str(params.get("candidate") or ""))
        row = db.execute(
            text("INSERT INTO bob_rows (table_id, position, cells) "
                 "VALUES (:t, :p, CAST(:c AS jsonb)) RETURNING id"),
            {"t": table_id, "p": pos + written + 1, "c": json.dumps(cells, ensure_ascii=False)},
        ).fetchone()
        db.commit()
        table_norms[o["company_norm"]] = row[0]
        state.transition(db, o["id"], "written", row_id=row[0])
        written += 1
    db.execute(text("UPDATE bob_tables SET updated_at = now() WHERE id = :t"), {"t": table_id})
    db.commit()
    state.push_event(db, run_id, "rows", f"Added {written} rows")
    backlog = sum(1 for o in pool[written:] if o["company_norm"] not in table_norms)
    return {"written": written, "backlog": backlog}
