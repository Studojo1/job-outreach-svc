"""Guards against the 23 Sep incident (audit CF-N05).

The code that read candidates.psychometric_profile went live 7.5 hours before
the migration that added the column. For those hours 26 resume uploads failed,
and because the handler returned str(e) as the error detail, 9 students saw raw
SQL on screen. Two guards:

  schema    At startup the pod compares every table and column the ORM maps
            with the live database and refuses to start if one is missing.
            Kubernetes then keeps the old pods serving, and the deploy
            workflow's `kubectl rollout status` fails, so a model can no longer
            go live ahead of its migration. Apply migrations first.

  5xx text  Any 5xx whose detail looks like a database or driver error is
            replaced with fixed, user-safe copy before it leaves the service
            (the text is logged). This catches every str(e) left anywhere.
"""

import logging

from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler

logger = logging.getLogger(__name__)


class SchemaMismatch(RuntimeError):
    pass


def missing_schema(engine, metadata) -> list[str]:
    """Tables and columns the ORM maps that the live database lacks."""
    from sqlalchemy import inspect
    insp = inspect(engine)
    live_tables = set(insp.get_table_names())
    missing = []
    for table in metadata.sorted_tables:
        if table.name not in live_tables:
            missing.append(f"table {table.name}")
            continue
        live_cols = {c["name"] for c in insp.get_columns(table.name)}
        missing += [f"column {table.name}.{c.name}" for c in table.columns if c.name not in live_cols]
    return missing


def check_schema(engine=None, metadata=None) -> None:
    """Raise SchemaMismatch if the database is behind the models. A database
    that cannot be reached is logged, not fatal: that is an outage, not a
    deploy ordering bug, and the pod's readiness will show it anyway."""
    if engine is None:
        from database.session import engine
    if metadata is None:
        from database.models import Base
        metadata = Base.metadata
    try:
        missing = missing_schema(engine, metadata)
    except Exception:
        logger.exception("[SCHEMA] could not inspect the database; skipping the schema check")
        return
    if missing:
        msg = ("The database is missing what the code maps: " + ", ".join(missing[:20])
               + ". Apply the pending migrations in migrations/ before deploying this build.")
        logger.critical("[SCHEMA] %s", msg)
        raise SchemaMismatch(msg)
    logger.info("[SCHEMA] database matches the models")


# Substrings that only ever appear in database / driver / Python internals.
_INTERNAL_MARKERS = (
    "[SQL:", "(Background on this error", "sqlalche.me", "psycopg", "sqlalchemy.",
    "UndefinedColumn", "UndefinedTable", "ProgrammingError", "IntegrityError",
    "OperationalError", "Traceback (most recent call last)",
)
SAFE_5XX_DETAIL = "Something went wrong on our side. Please try again in a moment."


def looks_internal(detail) -> bool:
    text = detail if isinstance(detail, str) else repr(detail)
    return any(m in text for m in _INTERNAL_MARKERS)


async def safe_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code >= 500 and looks_internal(exc.detail):
        logger.error("[SAFE_5XX] %s %s -> %s; internal detail withheld: %s",
                     request.method, request.url.path, exc.status_code, str(exc.detail)[:2000])
        exc = HTTPException(status_code=exc.status_code, detail=SAFE_5XX_DETAIL, headers=exc.headers)
    return await http_exception_handler(request, exc)
