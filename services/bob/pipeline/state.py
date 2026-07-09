"""Persistent pipeline state: raw harvest items + opportunity lifecycle.

Single write path for both tables. Rules enforced here, not by convention:
  - an opportunity can only become 'rejected' WITH a stage and a reason
    (work never silently disappears);
  - raw items dedupe on (chat_id, url_norm) so re-harvest and resume are
    idempotent;
  - funnel_snapshot() derives the run report from the DB, never from
    in-memory counters, so an interrupted run still reports truthfully.
"""

import json
import logging

from sqlalchemy import text

from services.bob.textrails import norm_company, norm_url

logger = logging.getLogger(__name__)

OPP_STATUSES = ("extracted", "verified", "scored", "contacted", "written", "rejected")

# Columns transition() may touch — anything else is a programming error.
_MUTABLE = {
    "status", "reject_stage", "reject_reason", "fit_score", "fit_reason",
    "contact_name", "contact_title", "contact_tier", "contact_source",
    "contact_profile_url", "contact_email", "needs_contact", "row_id",
    "website", "what_they_do", "extra",
}


def push_event(db, run_id: int, ev_type: str, label: str, detail: str = "", credits: int = 0) -> None:
    """Same event stream the frontend polls (mirrors agent._push_event)."""
    from datetime import datetime, timezone
    ev = {"ts": datetime.now(timezone.utc).isoformat(), "type": ev_type,
          "label": label[:160], "detail": detail[:400], "credits": credits}
    try:
        db.execute(
            text("UPDATE bob_runs SET events = events || CAST(:ev AS jsonb), "
                 "credits_used = credits_used + :credits, updated_at = now() WHERE id = :id"),
            {"ev": json.dumps([ev]), "credits": credits, "id": run_id},
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("[BOB/PIPE] push_event failed (run %s)", run_id, exc_info=True)


def save_params(db, run_id: int, params: dict) -> None:
    db.execute(
        text("UPDATE bob_runs SET params = params || CAST(:p AS jsonb), updated_at = now() WHERE id = :id"),
        {"p": json.dumps(params), "id": run_id},
    )
    db.commit()


def get_params(db, run_id: int) -> dict:
    return db.execute(text("SELECT params FROM bob_runs WHERE id = :id"), {"id": run_id}).scalar() or {}


def insert_raw_items(db, chat_id: int, run_id: int, source: str, query: str,
                     items: list[dict]) -> tuple[int, int]:
    """Persist harvested items; (chat_id, url_norm) dedupes across queries,
    sources and runs of the same chat. Returns (inserted, duplicates)."""
    inserted = 0
    for it in items:
        url = (it.get("url") or "").strip()
        if not url:
            continue
        res = db.execute(
            text(
                "INSERT INTO bob_raw_items (chat_id, run_id, source, query, url, url_norm, "
                "title, description, markdown) "
                "VALUES (:c, :r, :s, :q, :u, :un, :t, :d, :m) "
                "ON CONFLICT (chat_id, url_norm) DO NOTHING"
            ),
            {"c": chat_id, "r": run_id, "s": source, "q": query[:300], "u": url[:600],
             "un": norm_url(url)[:600], "t": (it.get("title") or "")[:300],
             "d": (it.get("description") or "")[:1000], "m": (it.get("markdown") or "")[:12000]},
        )
        inserted += res.rowcount or 0
    db.commit()
    return inserted, len(items) - inserted


def pending_raw(db, chat_id: int, limit: int = 200) -> list[dict]:
    """CHAT-scoped on purpose: a run that died mid-extract leaves items
    'harvested'; the next run in the chat picks them up (resumability)."""
    rows = db.execute(
        text("SELECT id, source, query, url, title, description, markdown "
             "FROM bob_raw_items WHERE chat_id = :c AND status = 'harvested' ORDER BY id LIMIT :l"),
        {"c": chat_id, "l": limit},
    ).fetchall()
    return [dict(zip(("id", "source", "query", "url", "title", "description", "markdown"), r)) for r in rows]


def mark_raw(db, item_id: int, status: str, note: str = "") -> None:
    db.execute(
        text("UPDATE bob_raw_items SET status = :s, note = :n WHERE id = :i"),
        {"s": status, "n": note[:300], "i": item_id},
    )
    db.commit()


def insert_opportunity(db, chat_id: int, run_id: int, raw_item_id: int | None, opp: dict) -> int:
    company = (opp.get("company") or "").strip()[:200]
    row = db.execute(
        text(
            "INSERT INTO bob_opportunities (chat_id, run_id, raw_item_id, company, company_norm, "
            "role, location, stipend, posted, website, evidence_url, source, evidence_quote, "
            "what_they_do, apply_email, apply_person, apply_url, author_name, author_headline, "
            "author_profile, author_affiliation, extra) "
            "VALUES (:c, :r, :ri, :co, :cn, :role, :loc, :st, :po, :web, :ev, :src, :quote, "
            ":wtd, :ae, :ap, :au, :an, :ah, :apr, :aa, CAST(:x AS jsonb)) RETURNING id"
        ),
        {
            "c": chat_id, "r": run_id, "ri": raw_item_id, "co": company,
            "cn": norm_company(company),
            "role": (opp.get("role") or "")[:200], "loc": (opp.get("location") or "")[:120],
            "st": (opp.get("stipend") or "")[:160], "po": (opp.get("posted") or "")[:80],
            "web": (opp.get("website") or "")[:300], "ev": (opp.get("evidence_url") or "")[:600],
            "src": (opp.get("source") or "")[:60], "quote": (opp.get("evidence_quote") or "")[:600],
            "wtd": (opp.get("what_they_do") or "")[:400],
            "ae": (opp.get("apply_email") or "")[:200], "ap": (opp.get("apply_person") or "")[:160],
            "au": (opp.get("apply_url") or "")[:600],
            "an": (opp.get("author_name") or "")[:160], "ah": (opp.get("author_headline") or "")[:300],
            "apr": (opp.get("author_profile") or "")[:400],
            "aa": (opp.get("author_affiliation") or "unknown")[:20],
            "x": json.dumps(opp.get("extra") or {}),
        },
    ).fetchone()
    db.commit()
    return row[0]


def opportunities(db, *, chat_id: int | None = None, run_id: int | None = None,
                  status: str | None = None, limit: int = 500) -> list[dict]:
    """Chat-scoped queries drive the stages (pending work survives run
    boundaries); run-scoped queries drive per-run reporting."""
    assert chat_id or run_id, "need chat_id or run_id"
    q = ("SELECT id, chat_id, run_id, raw_item_id, company, company_norm, role, location, "
         "stipend, posted, website, evidence_url, source, evidence_quote, what_they_do, "
         "apply_email, apply_person, apply_url, author_name, author_headline, author_profile, "
         "author_affiliation, status, reject_stage, reject_reason, fit_score, fit_reason, "
         "contact_name, contact_title, contact_tier, contact_source, contact_profile_url, "
         "contact_email, needs_contact, row_id, extra "
         "FROM bob_opportunities WHERE 1=1")
    params: dict = {"l": limit}
    if chat_id:
        q += " AND chat_id = :c"
        params["c"] = chat_id
    if run_id:
        q += " AND run_id = :r"
        params["r"] = run_id
    if status:
        q += " AND status = :s"
        params["s"] = status
    q += " ORDER BY id LIMIT :l"
    rows = db.execute(text(q), params).fetchall()
    keys = ("id", "chat_id", "run_id", "raw_item_id", "company", "company_norm", "role",
            "location", "stipend", "posted", "website", "evidence_url", "source",
            "evidence_quote", "what_they_do", "apply_email", "apply_person", "apply_url",
            "author_name", "author_headline", "author_profile", "author_affiliation",
            "status", "reject_stage", "reject_reason", "fit_score", "fit_reason",
            "contact_name", "contact_title", "contact_tier", "contact_source",
            "contact_profile_url", "contact_email", "needs_contact", "row_id", "extra")
    return [dict(zip(keys, r)) for r in rows]


def transition(db, opp_id: int, status: str, **fields) -> None:
    """Move an opportunity to a new status, optionally updating whitelisted
    columns. 'rejected' REQUIRES reject_stage and reject_reason."""
    assert status in OPP_STATUSES, f"unknown status {status!r}"
    if status == "rejected":
        assert fields.get("reject_stage") and fields.get("reject_reason"), \
            "rejected requires reject_stage and reject_reason"
    bad = set(fields) - _MUTABLE
    assert not bad, f"non-mutable columns: {bad}"
    sets = ["status = :status", "updated_at = now()"]
    params: dict = {"status": status, "id": opp_id}
    for k, v in fields.items():
        if k == "extra":
            sets.append("extra = extra || CAST(:extra AS jsonb)")
            params["extra"] = json.dumps(v)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    db.execute(text(f"UPDATE bob_opportunities SET {', '.join(sets)} WHERE id = :id"), params)
    db.commit()


def reject(db, opp_id: int, stage: str, reason: str) -> None:
    transition(db, opp_id, "rejected", reject_stage=stage, reject_reason=reason[:300])


def funnel_snapshot(db, run_id: int) -> dict:
    """Truthful run report straight from the DB."""
    raw = dict(db.execute(
        text("SELECT status, count(*) FROM bob_raw_items WHERE run_id = :r GROUP BY status"),
        {"r": run_id},
    ).fetchall())
    opp = dict(db.execute(
        text("SELECT status, count(*) FROM bob_opportunities WHERE run_id = :r GROUP BY status"),
        {"r": run_id},
    ).fetchall())
    vetoes = db.execute(
        text("SELECT reject_stage, reject_reason, count(*) FROM bob_opportunities "
             "WHERE run_id = :r AND status = 'rejected' "
             "GROUP BY reject_stage, reject_reason ORDER BY count(*) DESC LIMIT 30"),
        {"r": run_id},
    ).fetchall()
    tiers = dict(db.execute(
        text("SELECT coalesce(nullif(contact_source, ''), 'none'), count(*) "
             "FROM bob_opportunities WHERE run_id = :r AND status IN ('contacted', 'written') "
             "GROUP BY 1"),
        {"r": run_id},
    ).fetchall())
    return {
        "raw_items": raw,
        "opportunities": opp,
        "vetoes": [{"stage": s, "reason": rr, "count": c} for s, rr, c in vetoes],
        "contact_sources": tiers,
    }


def table_contact_owners(db, table_id: int) -> dict:
    """person_norm -> (company_norm, company_display) from existing table rows."""
    from services.bob.textrails import norm_person
    rows = db.execute(
        text("SELECT cells->>'company', cells->>'contact_name' FROM bob_rows WHERE table_id = :t"),
        {"t": table_id},
    ).fetchall()
    return {norm_person(cn): (norm_company(nm), nm) for nm, cn in rows if cn and nm}


def rejected_companies(db, chat_id: int) -> dict:
    """company_norm -> display name the user removed from this mandate
    (bob_rejections ledger; deletions are bans, not suggestions)."""
    rows = db.execute(
        text("SELECT company_norm, company FROM bob_rejections WHERE chat_id = :c"),
        {"c": chat_id},
    ).fetchall()
    return {n: c for n, c in rows}


def table_companies(db, table_id: int) -> dict:
    """company_norm -> row_id for dedupe against already-shipped rows."""
    rows = db.execute(
        text("SELECT id, cells->>'company' FROM bob_rows WHERE table_id = :t"),
        {"t": table_id},
    ).fetchall()
    return {norm_company(nm): rid for rid, nm in rows if nm}
