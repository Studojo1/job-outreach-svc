"""Mesa runner — execute a saved search across all its sources (scrape + dedupe + store)."""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database.mesa_models import MesaJob, MesaSearch
from services.mesa.sources import SOURCE_SCRAPERS

logger = logging.getLogger(__name__)


def run_search(db: Session, search: MesaSearch) -> dict:
    """Scrape every enabled source for one search; persist jobs not already stored."""
    sources = list(search.sources or ["linkedin"])
    scraped: list[dict] = []
    per_source: dict[str, int] = {}
    for src in sources:
        fn = SOURCE_SCRAPERS.get(src)
        if not fn:
            continue
        try:
            jobs = fn(
                search.keywords, search.location or "", search.date_posted or "24h",
                list(search.workplace_types or []), list(search.experience_levels or []), 60,
            )
        except Exception as e:  # noqa: BLE001 — one bad source must not sink the rest
            logger.error("[MESA] source %s failed for search %s: %s", src, search.id, e)
            jobs = []
        per_source[src] = len(jobs)
        for j in jobs:
            j["source"] = src
        scraped.extend(jobs)

    existing = {
        (r[0], r[1]) for r in
        db.query(MesaJob.source, MesaJob.linkedin_job_id).filter(MesaJob.search_id == search.id).all()
    }
    new = 0
    for j in scraped:
        eid = j.get("external_id")
        key = (j["source"], eid)
        if not eid or key in existing:
            continue
        db.add(MesaJob(
            search_id=search.id, source=j["source"], linkedin_job_id=eid,
            title=j.get("title"), company=j.get("company"), location=j.get("location"),
            posted_date=j.get("posted_date"), url=j.get("url"),
        ))
        existing.add(key)
        new += 1
    search.last_run_at = datetime.utcnow()
    db.commit()
    logger.info("[MESA] search %s: %d scraped %s, %d new", search.id, len(scraped), per_source, new)
    return {"scraped": len(scraped), "new": new, "by_source": per_source}


def run_due_searches(db: Session, min_hours: float = 20.0) -> dict:
    """Run every active search not run in the last `min_hours` (daily sweep)."""
    cutoff = datetime.utcnow() - timedelta(hours=min_hours)
    due = (
        db.query(MesaSearch)
        .filter(MesaSearch.is_active.is_(True))
        .filter((MesaSearch.last_run_at.is_(None)) | (MesaSearch.last_run_at < cutoff))
        .all()
    )
    total_new = 0
    for s in due:
        try:
            total_new += run_search(db, s)["new"]
        except Exception as e:  # noqa: BLE001
            logger.error("[MESA] run_search %s failed: %s", s.id, e)
            db.rollback()
    logger.info("[MESA] daily sweep: %d searches, %d new jobs", len(due), total_new)
    return {"searches_run": len(due), "new_jobs": total_new}
