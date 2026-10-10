"""Third-party removal requests (Privacy Policy v2.0 §5).

Someone a Studojo user wrote to emails admin@studojo.com. The admin logs it:

  1. log_request: the address is suppressed at once ("No Studojo user will be
     able to contact you through us again") and a request is opened with a
     deadline 30 days from when it was received.
  2. delete_data: their details are deleted from everything we hold about
     them as a contact (leads, sent-email recipients, extension drafts, the
     partner enrichment cache and Apollo reveals, phone enrichment and Bob
     contacts), and from then on only the hash of the address is kept, on the
     suppression entry and on the request.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import RemovalRequest
from services.email_campaign import suppression

logger = get_logger(__name__)

DEADLINE = timedelta(days=30)


class RemovalError(ValueError):
    pass


def valid_address(address: str) -> str:
    a = (address or "").strip().lower()
    if "@" not in a or " " in a or len(a) > 320 or a.startswith("@") or a.endswith("@"):
        raise RemovalError("Enter a valid email address.")
    return a


def log_request(db: Session, address: str, received_on: date, actor: str) -> RemovalRequest:
    a = valid_address(address)
    h = suppression.email_hash(a)
    suppression.suppress(db, a, f"removal request logged by {actor}", source="removal_request")
    existing = db.query(RemovalRequest).filter_by(email_hash=h, status="open").first()
    if existing:
        db.commit()
        return existing
    received = datetime.combine(received_on, time.min, tzinfo=timezone.utc)
    req = RemovalRequest(email=a, email_hash=h, received_at=received, deadline=received + DEADLINE,
                         status="open", created_at=datetime.now(timezone.utc))
    db.add(req)
    db.commit()
    logger.info("[REMOVAL] request %s logged by %s, due %s", req.id, actor, req.deadline.date())
    return req


def _cols(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _enrich_cache_hits(db: Session, a: str) -> list[str]:
    """api_enrich_cache keys whose cached result carries this address
    (result.emails.work / result.emails.personal, written by the frontend)."""
    rows = db.execute(
        text("SELECT linkedin_url, result FROM api_enrich_cache WHERE lower(CAST(result AS TEXT)) LIKE :p"),
        {"p": f"%{a}%"},
    ).all()
    hits = []
    for url, result in rows:
        r = result if isinstance(result, dict) else json.loads(result or "{}")
        emails = (r.get("emails") or {}).values() if isinstance(r.get("emails"), dict) else []
        if any((e or "").strip().lower() == a for e in emails):
            hits.append(url)
    return hits


def erase_contact(db: Session, address: str) -> dict:
    """Delete one contact's details everywhere we hold them. Caller commits."""
    a = valid_address(address)
    insp = inspect(db.connection())
    tables = set(insp.get_table_names())
    counts: dict[str, int] = {}
    p = {"e": a}

    linkedin_urls: set[str] = set()
    if "leads" in tables:
        linkedin_urls |= {u for (u,) in db.execute(
            text("SELECT linkedin_url FROM leads WHERE lower(trim(email)) = :e AND linkedin_url IS NOT NULL"), p
        ).all()}
        # Scrubbed, not deleted: the row anchors the student's own send history
        # and credit accounting. Everything that identifies the person goes.
        counts["leads"] = db.execute(text(
            "UPDATE leads SET email = NULL, email_verified = false, name = 'Removed contact', title = NULL, "
            "linkedin_url = NULL, location = NULL, apollo_id = NULL, status = 'removed' WHERE lower(trim(email)) = :e"
        ), p).rowcount
    if "emails_sent" in tables:
        counts["emails_cancelled"] = _cancel_pending(db, a)
        counts["emails_sent"] = db.execute(text(
            "UPDATE emails_sent SET to_email = NULL, reply_text = NULL WHERE lower(trim(to_email)) = :e"
        ), p).rowcount
    if "extension_drafts" in tables:
        counts["extension_drafts"] = db.execute(text(
            "UPDATE extension_drafts SET contact_email = NULL, contact_name = NULL, contact_title = NULL "
            "WHERE lower(trim(contact_email)) = :e"
        ), p).rowcount
    if "api_enrich_cache" in tables:
        keys = _enrich_cache_hits(db, a)
        linkedin_urls |= set(keys)
        n = 0
        for k in keys:
            n += db.execute(text("DELETE FROM api_enrich_cache WHERE linkedin_url = :k"), {"k": k}).rowcount
        counts["api_enrich_cache"] = n
    if "apollo_reveals" in tables:
        # apollo_reveals has no email column: its rows are keyed by the same
        # normalised LinkedIn URL as the cache entries found above.
        n = 0
        for u in linkedin_urls:
            norm = _norm_linkedin(u)
            n += db.execute(text("DELETE FROM apollo_reveals WHERE linkedin_url IN (:u, :n)"),
                            {"u": u, "n": norm}).rowcount
        counts["apollo_reveals"] = n
    for t in ("phone_enrich_results", "bob_t1_contacts"):
        if t in tables and "email" in _cols(insp, t):
            counts[t] = db.execute(text(f'DELETE FROM "{t}" WHERE lower(trim(email)) = :e'), p).rowcount  # noqa: S608
    return counts


PENDING = ("queued", "pending", "pending_enrichment", "followup_pending")


def _cancel_pending(db: Session, a: str) -> int:
    """Anything still waiting to go to this address is stopped, the same way
    the worker stops a suppressed one: a first touch fails and its credit
    returns, a follow-up is cancelled."""
    from sqlalchemy import func

    from database.models import EmailSent
    from services.email_campaign import outcomes

    rows = db.query(EmailSent).filter(
        func.lower(func.trim(EmailSent.to_email)) == a, EmailSent.status.in_(PENDING)
    ).all()
    for e in rows:
        if (e.followup_number or 0) > 0:
            e.status = "cancelled_reply"
            e.error_message = "Recipient asked to be removed"
            e.status_changed_at = datetime.utcnow()
        else:
            outcomes.fail(db, e, "Recipient asked to be removed")
    db.flush()
    return len(rows)


def _norm_linkedin(url: str) -> str:
    """frontend enrich.server.ts normalizeUrl"""
    import re

    m = re.search(r"linkedin\.com/in/([^/?#\s]+)", url or "", re.IGNORECASE)
    return f"linkedin.com/in/{m.group(1).lower()}" if m else (url or "").strip().lower()


def delete_data(db: Session, request_id: int, actor: str) -> dict:
    req = db.get(RemovalRequest, request_id)
    if req is None:
        raise LookupError(request_id)
    if req.status == "done" or not req.email:
        raise RemovalError("This request is already done; only the hash of the address is kept.")
    a = req.email
    try:
        counts = erase_contact(db, a)
        # Still blocked, by hash only from now on (§5).
        suppression.suppress(db, a, f"removal request {req.id}", source="removal_request")
        suppression.forget_plaintext(db, a)
        req.email = None
        req.status = "done"
        req.done_at = datetime.now(timezone.utc)
        req.done_by = actor
        req.result = counts
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info("[REMOVAL] request %s done by %s: %s", req.id, actor, counts)
    return counts


def as_item(req: RemovalRequest, now: Optional[datetime] = None) -> dict:
    def iso(v):
        return v.isoformat() if v else None
    return {
        "id": req.id,
        "email": req.email,
        "received_at": iso(req.received_at),
        "deadline": iso(req.deadline),
        "status": req.status,
        "done_at": iso(req.done_at),
    }
