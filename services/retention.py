"""Retention limits from Privacy Policy v2.0 §15.

  support_chat_logs      kept 12 months
  deleted_account_sends  kept 3 years after the account was deleted

Runs from the launch-nudge sweep (hourly, one replica at a time). A table
that does not exist yet (support_chat_logs is created lazily by the frontend,
deleted_account_sends by migration 055) is skipped.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from core.logger import get_logger

logger = get_logger(__name__)

SUPPORT_CHAT_MAX_AGE = timedelta(days=365)
DELETED_SENDS_MAX_AGE = timedelta(days=3 * 365)

# (table, timestamp column, max age, column is timestamptz)
RULES = (
    ("support_chat_logs", "created_at", SUPPORT_CHAT_MAX_AGE, True),
    ("deleted_account_sends", "deleted_at", DELETED_SENDS_MAX_AGE, False),
)


def run(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    tables = set(inspect(db.get_bind()).get_table_names())
    counts = {}
    for table, col, max_age, aware in RULES:
        if table not in tables:
            continue
        cutoff = now - max_age  # now is naive UTC
        if aware:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        n = db.execute(
            text(f'DELETE FROM "{table}" WHERE "{col}" < :cutoff'),  # noqa: S608 - names from RULES
            {"cutoff": cutoff},
        ).rowcount
        counts[table] = n
    db.commit()
    if any(counts.values()):
        logger.info("[RETENTION] deleted %s", counts)
    else:
        logger.info("[RETENTION] nothing past its retention period: %s", counts)
    return counts
