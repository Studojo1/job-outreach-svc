"""Re-extract resume_profile for candidates whose extraction never landed.

A candidate with resume text but no resume_profile is silently degraded
everywhere downstream: the quiz serves generic role options instead of ones
drawn from their actual experience, no archetype is available for the
personalised copy, and target_industries is derived from nothing.

Production at the time of writing: 1,108 of 6,744 candidates with usable resume
text have no profile (16.4%). That number is dominated by a historical spike —
May 2026 was 30.5% and June 24.5%, against 1.3-2.0% in every month before and
since — so it reads as an outage in that window rather than a steady failure
rate. Those rows were never retried, because the background task made one
attempt and swallowed whatever went wrong.

The live defect is fixed separately (extract_and_store_resume_profile now
retries three times and logs loudly when it gives up). This script repairs the
rows that predate that fix.

It is deliberately slow and resumable: each candidate is committed on its own,
so an interrupted run loses at most one row and re-running skips everything
already done. Each extraction is an LLM call, so --limit exists to do this in
affordable batches rather than 1,108 calls in one go.

Usage:
    python -m scripts.backfill_resume_profiles                  # dry run, counts only
    python -m scripts.backfill_resume_profiles --limit 25 --apply
    python -m scripts.backfill_resume_profiles --apply          # everything remaining
"""

from __future__ import annotations

import argparse
import logging
import time

logger = logging.getLogger("backfill_resume_profiles")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually extract and write (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="process at most this many candidates (0 = no limit)")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="skip resumes shorter than this; too little text to extract anything useful")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from database.session import SessionLocal
    from database.models import Candidate
    from sqlalchemy import func
    from services.candidate_intelligence.resume_intelligence import (
        extract_enhanced_resume_profile,
    )

    db = SessionLocal()
    try:
        q = (
            db.query(Candidate)
            .filter(
                Candidate.resume_profile.is_(None),
                Candidate.resume_text.isnot(None),
                func.length(func.trim(Candidate.resume_text)) >= args.min_chars,
            )
            .order_by(Candidate.created_at.desc())
        )

        total = q.count()
        logger.info("Candidates with resume text but no profile: %d", total)
        if args.limit:
            q = q.limit(args.limit)

        rows = q.all()
        logger.info("This run will process: %d", len(rows))

        if not args.apply:
            logger.info("")
            for c in rows[:15]:
                logger.info("  candidate=%s chars=%d created=%s",
                            c.id, len(c.resume_text or ""), c.created_at)
            if len(rows) > 15:
                logger.info("  ... and %d more", len(rows) - 15)
            logger.info("")
            logger.info("Dry run. Nothing written. Re-run with --apply to extract.")
            return

        ok = failed = 0
        for i, candidate in enumerate(rows, start=1):
            try:
                profile = extract_enhanced_resume_profile(resume_text=candidate.resume_text or "")
                if not isinstance(profile, dict) or not profile:
                    raise ValueError(f"extraction returned {type(profile).__name__}")
                candidate.resume_profile = profile
                # Commit per candidate: an interrupted run loses one row, not the batch.
                db.commit()
                ok += 1
                logger.info("[%d/%d] candidate=%s OK domain=%s archetype=%s",
                            i, len(rows), candidate.id,
                            profile.get("domain"), profile.get("archetype_label"))
            except Exception as exc:
                db.rollback()
                failed += 1
                logger.error("[%d/%d] candidate=%s FAILED %s: %s",
                             i, len(rows), candidate.id, type(exc).__name__, exc)
            # Be kind to the LLM endpoint; this is a bulk job, not a user request.
            time.sleep(0.5)

        logger.info("")
        logger.info("Extracted %d, failed %d, remaining overall %d", ok, failed, total - ok)

    finally:
        db.close()


if __name__ == "__main__":
    main()
