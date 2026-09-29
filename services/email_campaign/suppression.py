"""The one list of addresses Studojo never contacts again.

Audit P18 started it for bounces: suppressed_emails existed in production,
empty, and nothing read or wrote it, so 56 later sends went to addresses that
had already bounced. Since migration 055 it also holds everyone who asked to
be removed (Privacy Policy §5, Terms §6): a removal request logged by an
admin, an opt-out reply, or a manual entry.

Every lookup goes by email_hash, sha256 of the lowercased, trimmed address.
That lets an entry keep only the hash once the person's details are deleted
("we keep only a scrambled (hashed) copy of your address"), and it still
blocks them.

Paths that check this list: campaign first touch and follow-ups, extension
send-one and contact-check, Apollo enrichment (a suppressed address is never
stored on a lead), and send_gmail_email itself as the last line.
"""

import hashlib
import logging
import re
import time
import weakref
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SOURCES = ("bounce", "removal_request", "reply", "manual")


class SuppressedAddress(RuntimeError):
    """Raised by the last-line send guard for an address on the list."""


def _norm(address: Optional[str]) -> str:
    return (address or "").strip().lower()


def email_hash(address: Optional[str]) -> str:
    """sha256 hex of the normalised address; '' for an empty one."""
    a = _norm(address)
    return hashlib.sha256(a.encode("utf-8")).hexdigest() if a else ""


# Before migration 055 the table has no email_hash column. The code deploys
# on its own and the migration is applied by hand, so until it lands the
# lookup falls back to the plaintext column instead of failing every send.
# Cached per engine: once hashed, always hashed; otherwise re-probed.
_schema: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_RECHECK_SECONDS = 300


def _hashed_schema(db: Session) -> bool:
    bind = db.get_bind()
    hashed, checked_at = _schema.get(bind, (False, 0.0))
    if hashed:
        return True
    now = time.monotonic()
    if checked_at and now - checked_at < _RECHECK_SECONDS:
        return False
    try:
        cols = {c["name"] for c in inspect(db.connection()).get_columns("suppressed_emails")}
    except Exception as e:  # noqa: BLE001 - probe only
        logger.warning("[SUPPRESSION] schema probe failed: %s", e)
        cols = set()
    hashed = "email_hash" in cols
    _schema[bind] = (hashed, now)
    if not hashed:
        logger.warning("[SUPPRESSION] migration 055 not applied: checking plaintext addresses only")
    return hashed


def is_suppressed(db: Session, address: Optional[str]) -> bool:
    a = _norm(address)
    if not a:
        return False
    if _hashed_schema(db):
        return db.execute(
            text("SELECT 1 FROM suppressed_emails WHERE email_hash = :h"), {"h": email_hash(a)}
        ).first() is not None
    return db.execute(text("SELECT 1 FROM suppressed_emails WHERE email = :e"), {"e": a}).first() is not None


def suppress(db: Session, address: Optional[str], reason: str, source: str = "bounce") -> None:
    """Idempotent. Caller commits. The first entry's source wins."""
    a = _norm(address)
    if not a:
        return
    if source not in SOURCES:
        raise ValueError(f"unknown suppression source {source!r}")
    if _hashed_schema(db):
        db.execute(
            text("INSERT INTO suppressed_emails (email, email_hash, source, reason) VALUES (:e, :h, :s, :r) "
                 "ON CONFLICT (email_hash) DO NOTHING"),
            {"e": a, "h": email_hash(a), "s": source, "r": (reason or "")[:500]},
        )
        return
    db.execute(
        text("INSERT INTO suppressed_emails (email, reason) VALUES (:e, :r) ON CONFLICT (email) DO NOTHING"),
        {"e": a, "r": (reason or "")[:500]},
    )


def forget_plaintext(db: Session, address: Optional[str]) -> int:
    """Keep the hash, drop the readable address (§5). Caller commits."""
    a = _norm(address)
    if not a:
        return 0
    return db.execute(
        text("UPDATE suppressed_emails SET email = NULL WHERE email_hash = :h"), {"h": email_hash(a)}
    ).rowcount


def guard_send(address: Optional[str]) -> None:
    """Last line before a message leaves: raise if the address is on the list.

    Opens its own session so it works from any sender. Fails closed: if the
    list cannot be read, nothing is sent.
    """
    if not _norm(address):
        return
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        blocked = is_suppressed(db, address)
    except Exception as e:
        raise SuppressedAddress(f"could not check the suppression list: {e}") from e
    finally:
        db.close()
    if blocked:
        raise SuppressedAddress("recipient is on the suppression list")


# A reply asking us to stop. Deliberately narrow: "not interested" or "we're
# not hiring" is a no, not a request to be removed.
_REMOVAL_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[\s-]?out|"
    r"stop (?:emailing|contacting|messaging|sending)|"
    r"(?:do not|don'?t) (?:email|contact|message) me)\b",
    re.IGNORECASE,
)


def asks_removal(reply_text: Optional[str]) -> bool:
    """Only the top of the reply is read, so quoted history cannot trigger it."""
    body = (reply_text or "")
    cut = re.search(r"^\s*(>|On .+wrote:)", body, re.MULTILINE)
    if cut:
        body = body[:cut.start()]
    return bool(_REMOVAL_RE.search(body[:2000]))


def mask(address: Optional[str]) -> Optional[str]:
    """a•••@zomato.com"""
    a = _norm(address)
    if not a:
        return None
    local, _, domain = a.partition("@")
    return f"{local[:1]}•••@{domain}" if domain else f"{local[:1]}•••"
