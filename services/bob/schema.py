"""Bob (Mesa) schema bootstrap.

CI deploys never run migrations, so Bob provisions its own tables with
idempotent DDL on first router import. The DDL mirrors migrations/040_bob_mesa.sql
exactly — keep both in sync.
"""

import logging
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)

_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / "040_bob_mesa.sql"
_ensured = False


def ensure_schema() -> None:
    """Run the Bob DDL once per process. Safe to call repeatedly."""
    global _ensured
    if _ensured:
        return
    from database.session import SessionLocal

    raw = _MIGRATION_FILE.read_text()
    # Strip full-line SQL comments so fragments never begin with "--"
    ddl = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--"))
    db = SessionLocal()
    try:
        for statement in ddl.split(";"):
            stmt = statement.strip()
            if stmt:
                db.execute(text(stmt))
        db.commit()
        _ensured = True
        logger.info("[BOB] schema ensured")
    except Exception:
        db.rollback()
        logger.exception("[BOB] schema bootstrap failed")
        raise
    finally:
        db.close()
