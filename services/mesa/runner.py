"""Mesa runner — execute a saved search across all its sources (scrape + dedupe + store)."""

import logging
import re
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database.mesa_models import MesaJob, MesaSearch
from services.mesa import source_health
from services.mesa.postparse import posted_age_days
from services.mesa.sources import SOURCE_SCRAPERS

logger = logging.getLogger(__name__)

# The keywords field is often a comma/slash list of role variants ("SWE, SDE,
# Software Engineer"). Searching each term separately and merging finds far more
# than one combined query (matches what dedicated tools return). Capped to bound
# runtime per Run-now.
_MAX_TERMS = 8

# Sources that genuinely search server-side by the query — trust their results.
# The rest are aggregated FEEDS (jobicy/remotive/...) that return loosely- or
# un-filtered jobs (e.g. jobicy only tags on the first keyword token), so we
# relevance-filter their output against the searched term.
_TRUSTED_SEARCH = {"linkedin", "linkedin_posts", "getro", "indeed", "naukri"}


def _relevant(job: dict, term: str) -> bool:
    """Keep a feed job only if its title/company (or post text) actually relates
    to the searched term. Stem-aware (founders -> founder) so 'founders office'
    matches "Founder's Office" but drops "Customer Success Manager"."""
    toks = [t for t in re.sub(r"[^a-z0-9]", " ", (term or "").lower()).split() if len(t) >= 3]
    if not toks:
        return True
    hay = " " + re.sub(r"[^a-z0-9]", " ",
        f"{job.get('title', '')} {job.get('company', '')} {job.get('post_text', '') or ''}".lower()) + " "
    for t in toks:
        stem = t[:-1] if (t.endswith("s") and len(t) > 3) else t
        if f" {t}" in hay or f" {stem}" in hay:
            return True
    return False


# search date window -> max posting age (days) at ingest. Slack over the nominal
# window because sources bucket coarsely ("1 week ago" can mean 6-9 days).
_MAX_AGE = {"24h": 1.5, "week": 8.0, "month": 32.0, "any": None}

_LEGAL_RE = re.compile(r"\b(pvt|private|ltd|limited|inc|llc|llp|technologies|technology|labs|solutions|india)\b")


def _fingerprint(company: str, title: str) -> str:
    """Company+role-family key for cross-source corroboration. The same opening
    scraped from two sources (LinkedIn + Naukri, post + board) is one job — and
    independent confirmation is the strongest realness signal we have."""
    c = _LEGAL_RE.sub("", (company or "").lower())
    c = re.sub(r"[^a-z0-9]", "", c)[:12]
    t = re.sub(r"[^a-z0-9]", "", (title or "").lower())[:16]
    return f"{c}|{t}" if c and t else ""


def _terms(keywords: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[,/;|]| or | OR ", keywords or "") if p.strip()]
    seen: set = set()
    out: list[str] = []
    for p in parts:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out[:_MAX_TERMS] or [keywords or ""]


def run_search(db: Session, search: MesaSearch) -> dict:
    """Scrape every enabled source for one search across each keyword term;
    freshness-gate at ingest, corroborate duplicates across sources instead of
    duplicating rows, record per-source yield telemetry, persist new jobs."""
    sources = list(search.sources or ["linkedin"])
    terms = _terms(search.keywords)
    max_age = _MAX_AGE.get(search.date_posted or "24h", 8.0)
    scraped: list[dict] = []
    per_source: Counter = Counter()
    src_raw: Counter = Counter()
    src_errors: dict = {}
    for src in sources:
        fn = SOURCE_SCRAPERS.get(src)
        if not fn:
            continue
        src_seen: set = set()  # dedupe within a source across terms
        for term in terms:
            try:
                jobs = fn(
                    term, search.location or "", search.date_posted or "24h",
                    list(search.workplace_types or []), list(search.experience_levels or []), 150,
                )
            except Exception as e:  # noqa: BLE001 — one bad source/term must not sink the rest
                logger.error("[MESA] %s/%r failed for search %s: %s", src, term, search.id, e)
                src_errors[src] = str(e)[:300]
                jobs = []
            src_raw[src] += len(jobs)
            for j in jobs:
                eid = j.get("external_id")
                if not eid or eid in src_seen:
                    continue
                src_seen.add(eid)
                if src not in _TRUSTED_SEARCH and not _relevant(j, term):
                    continue  # feed board returned an off-keyword job — drop the noise
                # freshness gate: feed boards ignore date filters, so stale rows
                # (weeks/months old) land here looking fresh. Drop only when the
                # age is KNOWN to exceed the window — unknown ages pass through.
                age = posted_age_days(j.get("posted_date"))
                if max_age is not None and age is not None and age > max_age:
                    continue
                j["source"] = src
                scraped.append(j)
                per_source[src] += 1

    existing = {
        (r[0], r[1]) for r in
        db.query(MesaJob.source, MesaJob.linkedin_job_id).filter(MesaJob.search_id == search.id).all()
    }
    # fingerprint -> stored row, for cross-source corroboration
    fp_rows: dict = {}
    for row in db.query(MesaJob).filter(MesaJob.search_id == search.id).all():
        fp = _fingerprint(row.company or "", row.title or "")
        if fp and fp not in fp_rows:
            fp_rows[fp] = row
    new = 0
    corroborated = 0
    src_new: Counter = Counter()
    for j in scraped:
        eid = j.get("external_id")
        key = (j["source"], eid)
        if not eid or key in existing:
            continue
        fp = _fingerprint(j.get("company", ""), j.get("title", ""))
        prior = fp_rows.get(fp) if fp else None
        if prior is not None and prior.source != j["source"]:
            # same company+role from a DIFFERENT source: don't duplicate the row —
            # mark the confirmation and upgrade missing links on the stored copy
            confirmed = [x for x in (prior.corroborating_sources or "").split(",") if x]
            if j["source"] not in confirmed:
                confirmed.append(j["source"])
                prior.corroborating_sources = ",".join(confirmed)
                corroborated += 1
            if not prior.apply_link and j.get("apply_link"):
                prior.apply_link = j["apply_link"]
            if not prior.url and j.get("url"):
                prior.url = j["url"]
            existing.add(key)
            continue
        row = MesaJob(
            search_id=search.id, source=j["source"], linkedin_job_id=eid,
            title=j.get("title"), company=j.get("company"), location=j.get("location"),
            posted_date=j.get("posted_date"), url=j.get("url"),
            author=j.get("author"), apply_link=j.get("apply_link"), post_text=j.get("post_text"),
        )
        db.add(row)
        existing.add(key)
        if fp and fp not in fp_rows:
            fp_rows[fp] = row
        new += 1
        src_new[j["source"]] += 1
    for src in sources:
        if src in SOURCE_SCRAPERS:
            source_health.record_run(db, search.id, src, scraped=src_raw[src],
                                     kept=per_source[src], new_rows=src_new[src],
                                     error=src_errors.get(src))
    search.last_run_at = datetime.utcnow()
    db.commit()
    health = source_health.health_flags(db, search.id, sources)
    for flag in health:
        logger.warning("[MESA] source health: search %s %s -> %s (%s)",
                       search.id, flag["source"], flag["issue"], flag["detail"])
    logger.info("[MESA] search %s: %d scraped %s, %d new, %d corroborated",
                search.id, len(scraped), per_source, new, corroborated)
    return {"scraped": len(scraped), "new": new, "corroborated": corroborated,
            "by_source": per_source, "health": health}


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
