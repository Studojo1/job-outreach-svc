"""Mesa API — per-client saved LinkedIn job searches + scraped results.

Multi-tenant: every search is scoped to the authenticated user (the B2B client).
Cookie-free scraping (see services/mesa/linkedin_jobs.py). No Apollo.
"""

import csv
import io
import logging
import threading
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from database.mesa_models import MesaJob, MesaSearch
from database.models import User
from database.session import SessionLocal, get_db
from services.mesa.intelligence import attach_enrichment, enrichment_status, start_enrichment
from services.mesa.runner import run_due_searches, run_search
from services.mesa.signals import score_jobs
from services.mesa.sources import ALL_SOURCES, DEFAULT_SOURCES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mesa", tags=["Mesa"])

DATE_CHOICES = {"24h", "week", "month", "any"}
WORKPLACE_CHOICES = {"on-site", "remote", "hybrid"}
EXPERIENCE_CHOICES = {"internship", "entry", "associate", "mid-senior", "director", "executive"}


class SearchIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    keywords: str = Field(..., min_length=1, max_length=300)
    location: str = ""
    date_posted: str = "24h"
    workplace_types: List[str] = []
    experience_levels: List[str] = []
    sources: List[str] = []
    is_active: bool = True

    def clean(self) -> "SearchIn":
        if self.date_posted not in DATE_CHOICES:
            self.date_posted = "24h"
        self.workplace_types = [w for w in self.workplace_types if w in WORKPLACE_CHOICES]
        self.experience_levels = [e for e in self.experience_levels if e in EXPERIENCE_CHOICES]
        self.sources = [s for s in self.sources if s in ALL_SOURCES] or list(DEFAULT_SOURCES)
        return self


def _serialize(s: MesaSearch, job_count: int = 0) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "keywords": s.keywords,
        "location": s.location or "",
        "date_posted": s.date_posted or "24h",
        "workplace_types": list(s.workplace_types or []),
        "experience_levels": list(s.experience_levels or []),
        "sources": list(s.sources or []),
        "is_active": s.is_active,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "job_count": job_count,
    }


def _owned(db: Session, search_id: int, user: User) -> MesaSearch:
    s = db.query(MesaSearch).filter(MesaSearch.id == search_id).first()
    if not s:
        raise HTTPException(404, "Search not found")
    if s.user_id != user.id:
        raise HTTPException(403, "Not your search")
    return s


# ── Searches CRUD ──────────────────────────────────────────────────────────────
@router.get("/searches")
async def list_searches(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(MesaSearch).filter(MesaSearch.user_id == current_user.id).order_by(MesaSearch.id.desc()).all()
    counts = dict(
        db.query(MesaJob.search_id, func.count(MesaJob.id))
        .filter(MesaJob.search_id.in_([r.id for r in rows] or [0]))
        .group_by(MesaJob.search_id).all()
    )
    return {"searches": [_serialize(r, counts.get(r.id, 0)) for r in rows]}


@router.post("/searches")
async def create_search(body: SearchIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    body.clean()
    s = MesaSearch(
        user_id=current_user.id, name=body.name, keywords=body.keywords, location=body.location,
        date_posted=body.date_posted, workplace_types=body.workplace_types,
        experience_levels=body.experience_levels, sources=body.sources, is_active=body.is_active,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _serialize(s, 0)


@router.put("/searches/{search_id}")
async def update_search(search_id: int, body: SearchIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = _owned(db, search_id, current_user)
    body.clean()
    s.name, s.keywords, s.location = body.name, body.keywords, body.location
    s.date_posted, s.workplace_types, s.experience_levels = body.date_posted, body.workplace_types, body.experience_levels
    s.sources = body.sources
    s.is_active = body.is_active
    db.commit()
    return _serialize(s)


@router.delete("/searches/{search_id}")
async def delete_search(search_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = _owned(db, search_id, current_user)
    db.delete(s)
    db.commit()
    return {"status": "deleted", "id": search_id}


def _run_search_bg(search_id: int):
    """Background worker: deep multi-source scrape can take 1-3 min, so run it off
    the request thread with its own DB session. Progress is observable via the
    search's last_run_at / job_count (DB-backed, works across replicas)."""
    db = SessionLocal()
    try:
        s = db.query(MesaSearch).filter(MesaSearch.id == search_id).first()
        if s:
            run_search(db, s)
    except Exception as e:  # noqa: BLE001
        logger.error("[MESA] background run %s failed: %s", search_id, e)
    finally:
        db.close()


@router.post("/searches/{search_id}/run")
async def run_now(search_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = _owned(db, search_id, current_user)
    threading.Thread(target=_run_search_bg, args=(s.id,), daemon=True).start()
    return {"status": "started"}


# ── Jobs (results) ───────────────────────────────────────────────────────────────
@router.get("/searches/{search_id}/jobs")
async def list_jobs(
    search_id: int,
    q: Optional[str] = Query(None, description="filter by title/company text"),
    sort: str = Query("scraped", pattern="^(scraped|posted|company|title)$"),
    limit: int = Query(200, le=1000),
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned(db, search_id, current_user)
    query = db.query(MesaJob).filter(MesaJob.search_id == search_id)
    if q:
        like = f"%{q}%"
        query = query.filter((MesaJob.title.ilike(like)) | (MesaJob.company.ilike(like)))
    order = {
        "scraped": MesaJob.scraped_at.desc(),
        "posted": MesaJob.posted_date.desc(),
        "company": MesaJob.company.asc(),
        "title": MesaJob.title.asc(),
    }[sort]
    total = query.count()
    rows = query.order_by(order).offset(offset).limit(limit).all()
    return {
        "total": total,
        "jobs": [{
            "id": j.id, "title": j.title, "company": j.company, "location": j.location,
            "posted_date": j.posted_date, "url": j.url, "source": j.source,
            "author": j.author, "apply_link": j.apply_link, "post_text": j.post_text,
            "scraped_at": j.scraped_at.isoformat() if j.scraped_at else None,
        } for j in rows],
    }


@router.get("/searches/{search_id}/jobs.csv")
async def export_jobs_csv(search_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = _owned(db, search_id, current_user)
    rows = db.query(MesaJob).filter(MesaJob.search_id == search_id).order_by(MesaJob.scraped_at.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["title", "company", "author", "apply_link", "linkedin_url", "location",
                "posted_date", "source", "post_text", "scraped_at"])
    for j in rows:
        w.writerow([j.title, j.company, j.author, j.apply_link, j.url, j.location,
                    j.posted_date, j.source, j.post_text,
                    j.scraped_at.isoformat() if j.scraped_at else ""])
    buf.seek(0)
    fname = f"mesa_{s.name.replace(' ', '_')[:40]}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ── Signals (the intelligence layer over a search's results) ─────────────────────
@router.get("/searches/{search_id}/signals")
async def search_signals(
    search_id: int,
    limit: int = Query(50, le=200),
    confluence_only: bool = Query(False, description="only companies emitting 2+ independent signal families"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Score every company in this search's scraped jobs across independent signal
    families (leadership seat open, army-no-general, RevOps, founder hiring post,
    'no leader yet' language, hiring surge) and rank by CONFLUENCE — 2+ families is
    a real buying signal, one alone is usually noise. Works over any source."""
    _owned(db, search_id, current_user)
    rows = db.query(MesaJob).filter(MesaJob.search_id == search_id).all()
    jobs = [{
        "title": j.title, "company": j.company, "location": j.location,
        "posted_date": j.posted_date, "url": j.url, "source": j.source, "post_text": j.post_text,
    } for j in rows]
    ranked = score_jobs(jobs)
    # fold in any background enrichment (facts + news signals + analyst brief) and re-score
    ranked = attach_enrichment(search_id, ranked)
    if confluence_only:
        ranked = [r for r in ranked if r["confluence"]]
    return {
        "total_companies": len(ranked),
        "confluence_count": sum(1 for r in ranked if r["confluence"]),
        "enrichment": enrichment_status(search_id),
        "companies": ranked[:limit],
    }


@router.post("/searches/{search_id}/signals/enrich")
async def enrich_signals(
    search_id: int,
    limit: int = Query(15, le=40, description="how many top-ranked companies to deep-enrich"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Kick off BACKGROUND deep enrichment for the top-ranked companies: company
    facts (LLM + web, no Apollo), fresh-funding + leader-departure news signals,
    and an analyst brief (confidence, signal narrative, outreach opener, kill-signal
    check). Poll GET /signals to see results fill in (`enrichment` field shows
    progress; enriched companies gain `facts` / `brief` / news signals and re-rank)."""
    _owned(db, search_id, current_user)
    rows = db.query(MesaJob).filter(MesaJob.search_id == search_id).all()
    jobs = [{
        "title": j.title, "company": j.company, "location": j.location,
        "posted_date": j.posted_date, "url": j.url, "source": j.source, "post_text": j.post_text,
    } for j in rows]
    ranked = score_jobs(jobs)[:limit]
    start_enrichment(search_id, [{"company": r["company"], "families": r["families"]} for r in ranked])
    return {"status": "started", "enriching": len(ranked)}


# ── Internal: daily sweep (cluster-only, no auth — called by the CronJob) ─────────
@router.post("/worker/run-due")
def worker_run_due(db: Session = Depends(get_db)):
    """Run all active searches due for a refresh. No auth: only reachable in-cluster."""
    try:
        return run_due_searches(db)
    except Exception as e:  # noqa: BLE001
        logger.error("[MESA] run-due failed: %s", e, exc_info=True)
        raise HTTPException(500, str(e))
