"""One-off for UC-Q03: fill blank company fields on leads already stored.

New leads are filled from the company_profiles cache when they are stored and
again before they are scored (services/company_intelligence/lead_backfill.py).
This does the same for leads stored before that shipped: industry, company
size, company description and company domain, blank fields only, from the
cache only (no Apollo, no web research).

Dry run by default: every batch is rolled back and only the counts are
printed. --apply commits batch by batch.

Usage: python -m scripts.backfill_lead_company_fields [--apply] [--days 30]
       [--candidate-id N] [--batch 2000]
"""

import argparse
import sys
from datetime import datetime, timedelta

from sqlalchemy import or_

from database.models import Lead
from database.session import SessionLocal
from services.company_intelligence.lead_backfill import backfill_leads_from_cache


def _needs_fill():
    return or_(
        Lead.industry.is_(None), Lead.industry == "",
        Lead.company_size.is_(None), Lead.company_size == "",
        Lead.company_description.is_(None), Lead.company_description == "",
        Lead.company_domain.is_(None), Lead.company_domain == "",
    )


def run(db, apply: bool, days: int | None = 30, candidate_id: int | None = None,
        batch: int = 2000) -> dict:
    out = {"scanned": 0, "filled": 0}
    q = db.query(Lead).filter(_needs_fill(), Lead.company.isnot(None))
    if days:
        q = q.filter(Lead.created_at >= datetime.utcnow() - timedelta(days=days))
    if candidate_id:
        q = q.filter(Lead.candidate_id == candidate_id)

    last_id = 0
    while True:
        rows = q.filter(Lead.id > last_id).order_by(Lead.id).limit(batch).all()
        if not rows:
            break
        last_id = rows[-1].id
        out["scanned"] += len(rows)
        out["filled"] += backfill_leads_from_cache(db, rows)
        if apply:
            db.commit()
        else:
            db.rollback()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=30, help="only leads created in the last N days (0 = all)")
    ap.add_argument("--candidate-id", type=int, default=None)
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args()
    db = SessionLocal()
    try:
        out = run(db, apply=args.apply, days=args.days or None,
                  candidate_id=args.candidate_id, batch=args.batch)
    finally:
        db.close()
    print(("APPLIED: " if args.apply else "DRY RUN (rolled back): ") +
          ", ".join(f"{k}={v}" for k, v in out.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
