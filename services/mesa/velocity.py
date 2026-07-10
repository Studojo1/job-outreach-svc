"""Job-posting-velocity signal — a company opening many new roles at once is scaling hard and is a
hot, reachable lead. Derived entirely from jobs Mesa already stores in `mesa_jobs` (no new table):
for each company we take the FIRST time each distinct role was seen (min scraped_at), then compare how
many roles first appeared in the recent window vs the prior window. A jump = a SURGE.

Value grows with history — the first ~2 weeks of a search build the baseline; surges show after that.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.mesa_models import MesaJob, MesaSearch

logger = logging.getLogger(__name__)


def compute_company_velocity(
    db: Session, user_id: str, window_days: int = 7, min_recent: int = 5, ratio: float = 1.5
) -> list[dict]:
    """Per-company posting velocity for one B2B client's searches.

    Returns rows sorted by recent-new desc:
        {company, recent, prior, delta, surge, sample_roles}
    - recent : distinct roles first seen in the last `window_days`
    - prior  : distinct roles first seen in the `window_days` before that
    - surge  : recent >= min_recent AND (prior == 0 or recent/prior >= ratio)
    """
    search_ids = [s.id for s in db.query(MesaSearch.id).filter(MesaSearch.user_id == user_id).all()]
    if not search_ids:
        return []

    now = datetime.utcnow()
    recent_cut = now - timedelta(days=window_days)
    prior_cut = now - timedelta(days=2 * window_days)

    # first time each distinct role (company, source, external id) was seen
    first_seen = (
        db.query(
            MesaJob.company.label("company"),
            func.min(MesaJob.scraped_at).label("first_at"),
            func.min(MesaJob.title).label("title"),
        )
        .filter(MesaJob.search_id.in_(search_ids), MesaJob.company.isnot(None), MesaJob.company != "")
        .group_by(MesaJob.company, MesaJob.source, MesaJob.linkedin_job_id)
        .subquery()
    )

    agg: dict[str, dict] = {}
    for company, first_at, title in db.query(first_seen.c.company, first_seen.c.first_at, first_seen.c.title):
        if not first_at:
            continue
        a = agg.setdefault(company, {"recent": 0, "prior": 0, "sample_roles": []})
        if first_at >= recent_cut:
            a["recent"] += 1
            if len(a["sample_roles"]) < 3 and title:
                a["sample_roles"].append(title)
        elif first_at >= prior_cut:
            a["prior"] += 1

    out = []
    for company, a in agg.items():
        recent, prior = a["recent"], a["prior"]
        if recent == 0:
            continue
        surge = recent >= min_recent and (prior == 0 or recent / max(prior, 1) >= ratio)
        out.append({
            "company": company, "recent": recent, "prior": prior, "delta": recent - prior,
            "surge": surge, "sample_roles": a["sample_roles"],
        })
    out.sort(key=lambda r: (not r["surge"], -r["recent"]))
    logger.info("[MESA] velocity: %d companies, %d surging", len(out), sum(1 for r in out if r["surge"]))
    return out
