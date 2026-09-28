"""Addresses we must not email again (audit P18).

suppressed_emails existed in production, empty, and nothing read or wrote
it. A hard bounce was recorded on one row and forgotten: 56 later sends went
to addresses that had already bounced (34 of them follow-ups into a thread
that bounced), and five student mailboxes ran bounce rates of 4-9.5%, which is
what gets a personal Gmail throttled.
"""

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


def _norm(address: Optional[str]) -> str:
    return (address or "").strip().lower()


def is_suppressed(db: Session, address: Optional[str]) -> bool:
    a = _norm(address)
    if not a:
        return False
    return db.execute(text("SELECT 1 FROM suppressed_emails WHERE email = :e"), {"e": a}).first() is not None


def suppress(db: Session, address: Optional[str], reason: str) -> None:
    """Idempotent. Caller commits."""
    a = _norm(address)
    if not a:
        return
    db.execute(
        text("INSERT INTO suppressed_emails (email, reason) VALUES (:e, :r) ON CONFLICT (email) DO NOTHING"),
        {"e": a, "r": (reason or "")[:500]},
    )
