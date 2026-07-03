"""Cross-source hiring-signal fusion for Mesa — the "deeply integrated" layer.

A single listing is weak evidence. The strong signal is corroboration: when the
*same company* shows up hiring across several independent sources (their ATS +
LinkedIn + a founder's feed post + an HN "who's hiring" comment), that's a
company genuinely ramping — and often with a reachable contact.

`fuse()` runs a set of sources for a query, normalises company names, and returns
one row per company with: which sources saw them, how many roles, sample titles,
any direct apply link/email, and a confidence score. Useful for both lenses:
- candidate: prioritise corroborated, contactable openings.
- B2B: rank companies actively ramping = warm placement/sales leads.

Not a SOURCE_SCRAPERS entry — it's an aggregator over them.
"""
import logging
import re
from collections import defaultdict

from services.mesa.sources import SOURCE_SCRAPERS

logger = logging.getLogger(__name__)

# Fast, reliable, no-Playwright sources make a good fusion set by default.
FUSION_SOURCES = ["linkedin", "greenhouse", "ashby", "lever", "hackernews",
                  "internshala", "himalayas", "remotive", "remoteok", "jobicy"]
_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|pvt|private|technologies|technology|"
                     r"labs|systems|solutions|software|global|corp|co|group|india|"
                     r"studios|ventures|the)\b", re.I)
_APPLY = re.compile(r"(mailto:[^\s]+|https?://[^\s]+)", re.I)


def _norm(company: str) -> str:
    c = re.sub(r"[^a-z0-9 ]", " ", (company or "").lower())
    c = _SUFFIX.sub(" ", c)
    return re.sub(r"\s+", " ", c).strip()


def fuse(keywords: str, date_posted: str = "week", sources: list[str] | None = None,
         per_source: int = 60, min_sources: int = 1) -> list[dict]:
    srcs = sources or FUSION_SOURCES
    by_company: dict[str, dict] = defaultdict(
        lambda: {"company": "", "sources": set(), "roles": 0, "titles": [],
                 "apply": set(), "urls": set(), "direct": False})
    for src in srcs:
        fn = SOURCE_SCRAPERS.get(src)
        if not fn:
            continue
        try:
            jobs = fn(keywords, "", date_posted, [], [], per_source)
        except Exception as e:  # noqa: BLE001
            logger.warning("[MESA] fuse/%s: %s", src, e)
            continue
        for j in jobs:
            key = _norm(j.get("company") or "")
            if not key or key in ("-", "see post", "hiring post"):
                continue
            e = by_company[key]
            e["company"] = e["company"] or (j.get("company") or "").strip()
            e["sources"].add(src)
            e["roles"] += 1
            if j.get("title") and len(e["titles"]) < 6:
                e["titles"].append(j["title"])
            if j.get("url"):
                e["urls"].add(j["url"])
            # direct-contact signals live in feed-post extras
            apply = j.get("apply_link") or ""
            if apply:
                e["apply"].add(apply)
            if apply.startswith("mailto:") or src == "linkedin_posts":
                e["direct"] = True

    out = []
    for key, e in by_company.items():
        nsrc = len(e["sources"])
        if nsrc < min_sources:
            continue
        # confidence: breadth of corroboration dominates, then volume, then reachability
        conf = nsrc * 25 + min(e["roles"], 10) * 3 + (15 if e["direct"] else 0)
        out.append({
            "company": e["company"], "confidence": min(100, conf),
            "source_count": nsrc, "sources": sorted(e["sources"]),
            "open_roles": e["roles"], "sample_titles": e["titles"],
            "apply": sorted(e["apply"])[:3], "has_direct_contact": e["direct"],
            "link": sorted(e["urls"])[0] if e["urls"] else "",
        })
    out.sort(key=lambda r: (-r["source_count"], -r["confidence"], -r["open_roles"]))
    logger.info("[MESA] fuse(%r): %d companies, %d multi-source",
                keywords, len(out), sum(1 for r in out if r["source_count"] > 1))
    return out
