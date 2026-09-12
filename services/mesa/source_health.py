"""Per-source yield telemetry for Mesa runs (migration 043).

The nastiest sourcing failure is silent: a source breaks (DOM change, IP
throttle, expired burner cookie) and simply returns 0 forever, quietly starving
every search that relies on it — the remaining sources' noise becomes the
"backbone" and nobody notices. Recording per-run yields makes that visible as a
zero-yield streak on a source that used to produce.
"""

import logging

from sqlalchemy.orm import Session

from database.mesa_models import MesaSourceRun

logger = logging.getLogger(__name__)

# consecutive zero-yield runs on a previously-producing source before we flag it
_STREAK = 3


def record_run(db: Session, search_id: int, source: str,
               scraped: int, kept: int, new_rows: int, error: str | None = None) -> None:
    """Append one source-run record; never raises (telemetry must not sink a run)."""
    try:
        db.add(MesaSourceRun(search_id=search_id, source=source, scraped=scraped,
                             kept=kept, new_rows=new_rows, error=(error or None)))
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA_HEALTH] record_run failed for %s: %s", source, e)


def health_flags(db: Session, search_id: int, sources: list[str]) -> list[dict]:
    """Return alert dicts for sources that look broken for this search:
    N consecutive zero-yield runs after having produced before, or a run error."""
    flags: list[dict] = []
    for src in sources:
        runs = (db.query(MesaSourceRun)
                .filter(MesaSourceRun.search_id == search_id, MesaSourceRun.source == src)
                .order_by(MesaSourceRun.ran_at.desc())
                .limit(_STREAK + 12).all())
        if not runs:
            continue
        recent = runs[:_STREAK]
        if runs[0].error:
            flags.append({"source": src, "issue": "error", "detail": (runs[0].error or "")[:200]})
            continue
        if (len(recent) == _STREAK
                and all(r.scraped == 0 for r in recent)
                and any(r.scraped > 0 for r in runs[_STREAK:])):
            flags.append({"source": src, "issue": "zero_yield_streak",
                          "detail": f"0 rows for {_STREAK} consecutive runs after producing before"})
    return flags
