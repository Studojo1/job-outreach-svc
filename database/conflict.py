"""INSERT ... ON CONFLICT DO NOTHING for the dialects we run on.

UC-Q36: leads(candidate_id, apollo_id) and lead_scores(lead_id) get UNIQUE
indexes. Two concurrent discovery or scoring passes both pass a
check-then-insert, so the second insert must be skipped rather than raise.

No conflict target is named, so this is also valid before the indexes exist
(Postgres rejects ON CONFLICT (cols) without a matching unique index).
"""
from sqlalchemy.orm import Session


def insert_ignore(db: Session, model, rows: list[dict], returning=None) -> list:
    """Insert rows, silently skipping any that violate a unique index.

    Returns the `returning` columns of the rows actually inserted (empty list
    when nothing was requested or everything conflicted).
    """
    if not rows:
        return []
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - we only run on these two
        raise NotImplementedError(f"insert_ignore: unsupported dialect {dialect}")
    stmt = insert(model.__table__).values(rows).on_conflict_do_nothing()
    if returning is not None:
        return list(db.execute(stmt.returning(*returning)).all())
    db.execute(stmt)
    return []
