"""Company-level growth / hiring-intent signals for Mesa (the B2B lens).

A single job listing is a role. A company that just opened *many* roles this week
is a company that's ramping — a warm lead for placement/sales. This derives that
signal in-house from the ATS scrapers (ats.py), which read each company's own
board straight from the source, so counts are exact.

`hiring_surge()` returns company-level rows (not the job-dict shape), so it is a
utility for the B2B side / a dedicated endpoint — NOT registered in
SOURCE_SCRAPERS. Funding-round signals need an external data source (Crunchbase/
news API); left as a documented follow-up rather than a flaky scraper.
"""
import logging
from collections import defaultdict

from services.mesa import ats

logger = logging.getLogger(__name__)

_ATS = {
    "greenhouse": ats.scrape_greenhouse,
    "ashby": ats.scrape_ashby,
    "lever": ats.scrape_lever,
    "workable": ats.scrape_workable,
}


def hiring_surge(keywords: str = "", date_posted: str = "week", min_roles: int = 3,
                 max_per_ats: int = 400) -> list[dict]:
    """Companies with >= min_roles openings in the window. Sorted by role count.

    Returns [{company, ats, open_roles, sample_titles, careers_hint}].
    """
    by_company: dict[tuple, list[dict]] = defaultdict(list)
    for name, fn in _ATS.items():
        try:
            jobs = fn(keywords, "", date_posted, [], [], max_per_ats)
        except Exception as e:  # noqa: BLE001
            logger.warning("[MESA] growth/%s: %s", name, e)
            continue
        for j in jobs:
            by_company[(j.get("company") or "-", name)].append(j)

    out = []
    for (company, name), jobs in by_company.items():
        if len(jobs) < min_roles:
            continue
        out.append({
            "company": company, "ats": name, "open_roles": len(jobs),
            "sample_titles": [j["title"] for j in jobs[:5]],
            "careers_hint": jobs[0].get("url", "").split("/jobs")[0] if jobs else "",
        })
    out.sort(key=lambda r: -r["open_roles"])
    logger.info("[MESA] hiring_surge(%r): %d companies >= %d roles", keywords, len(out), min_roles)
    return out
