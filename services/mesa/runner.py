"""Mesa runner — execute a saved search (scrape + dedupe + store) and the daily sweep."""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database.mesa_models import MesaJob, MesaSearch
from services.mesa.linkedin_jobs import scrape_jobs

logger = logging.getLogger(__name__)


def run_search(db: Session, search: MesaSearch) -> dict:
    """Scrape one search and persist only jobs not already stored for it."""
    jobs = scrape_jobs(
        keywords=search.keywords,
        location=search.location or "",
        date_posted=search.date_posted or "24h",
        workplace_types=list(search.workplace_types or []),
        experience_levels=list(search.experience_levels or []),
    )
    existing = {
        r[0] for r in db.query(MesaJob.linkedin_job_id)
        .filter(MesaJob.search_id == search.id).all()
    }
    new = 0
    for j in jobs:
        if j["linkedin_job_id"] in existing:
            continue
        db.add(MesaJob(
            search_id=search.id,
            linkedin_job_id=j["linkedin_job_id"],
            title=j["title"], company=j["company"], location=j["location"],
            posted_date=j.get("posted_date"), url=j["url"],
        ))
        existing.add(j["linkedin_job_id"])
        new += 1
    search.last_run_at = datetime.utcnow()
    db.commit()
    logger.info("[MESA] search %s (%s): %d scraped, %d new", search.id, search.name, len(jobs), new)
    return {"scraped": len(jobs), "new": new}


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
        except Exception as e:  # noqa: BLE001 — one bad search must not stop the sweep
            logger.error("[MESA] run_search %s failed: %s", s.id, e)
            db.rollback()
    logger.info("[MESA] daily sweep: %d searches, %d new jobs", len(due), total_new)
    return {"searches_run": len(due), "new_jobs": total_new}
