"""Mesa Signal Engine — turn a search's scraped jobs into ranked company SIGNALS.

General-purpose (works for ANY search, not just sales): a single posting is noise,
but the combination of independent signals a company emits is intent. Scores every
company across independent families and rewards CONFLUENCE (2+ = a real signal).
Pure function over the standard Mesa job dicts — no external calls, no auth.

Signal families (all keyword-agnostic — the jobs are already the search's matches):
  multi_role       hiring 3+ roles you're tracking = actively scaling in your area
  leadership_open  a Head/VP/Director/Chief/Lead/Chief-of-Staff role is open
  founder_post     a role came from a founder/recruiter hiring post (high intent)
  fresh            a role was posted in the last ~10 days
  multi_source     the company shows up from 2+ sources (corroboration)
  sales_build      (sales searches only) hiring junior sales but no sales leader
  revops           (sales searches only) a RevOps / Sales Ops hire = leader follows
Plus enrichment families folded in later by intelligence.attach_enrichment
(fresh_funding, leader_departure, enterprise_motion, enterprise_ready).
"""

import re
from collections import defaultdict
from datetime import datetime, timezone

_LEADER = re.compile(
    r"\b(head of|head[,\- ]|vp\b|v\.p|vice president|director of|director[,\- ]|chief|"
    r"cro\b|cmo\b|cto\b|cfo\b|coo\b|chief of staff|founder'?s office|country head|country manager|"
    r"national head|regional head|global head|svp|principal|\blead\b)\b", re.I)
_SALES = re.compile(r"\b(sales|revenue|gtm|go[- ]to[- ]market|business development|account executive|\bae\b|quota)\b", re.I)
_JUNIOR_SALES = re.compile(r"\b(sdr|bdr|account executive|\bae\b|inside sales|sales development|sales executive|business development (rep|associate|executive)|sales associate)\b", re.I)
_SALES_LEADER = re.compile(r"\b(head|vp|vice president|director|chief|cro)\b.*\b(sales|revenue|gtm|commercial)\b", re.I)
_REVOPS = re.compile(r"\b(revenue operations|revops|rev ops|sales operations|sales ops|gtm operations|sales enablement|deal desk)\b", re.I)

_FAMILY_WEIGHT = {
    "multi_role": 18, "leadership_open": 16, "founder_post": 22, "fresh": 8,
    "multi_source": 6, "sales_build": 12, "revops": 8,
}
_FAMILY_LABEL = {
    "multi_role": "Hiring surge (3+ roles you track)",
    "leadership_open": "Leadership role open",
    "founder_post": "Founder/recruiter hiring post (high intent)",
    "fresh": "Fresh posting (last ~10 days)",
    "multi_source": "Corroborated across 2+ sources",
    "sales_build": "Building a sales team, no leader yet",
    "revops": "RevOps hire (systematizing sales)",
}
# families that are meaningful enough to count toward confluence (exclude the two
# soft ones so 'fresh + multi_source' alone isn't treated as strong intent)
_CORE = {"multi_role", "leadership_open", "founder_post", "sales_build", "revops"}


def _norm(name: str) -> str:
    n = re.sub(r"\(.*?\)", " ", (name or "").lower())
    n = re.sub(r"\b(ai|technologies|technology|labs|inc|india|pvt|ltd|the|app|cloud|global|hq)\b", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def _is_fresh(posted: str) -> bool:
    if not posted:
        return False
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(posted))
    if not m:
        return False
    try:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days <= 10
    except Exception:  # noqa: BLE001
        return False


def _read(fams: set, lead_title: str, n_roles: int) -> str:
    if "founder_post" in fams:
        return "A founder/recruiter is personally posting this hire — reach them directly, the intent is explicit."
    bits = []
    if "leadership_open" in fams and lead_title:
        bits.append(f"Opening a leadership role ({lead_title[:40]}) — they're building this function out.")
    if "multi_role" in fams:
        bits.append(f"Hiring {n_roles} roles you're tracking — actively scaling in your area.")
    if "sales_build" in fams:
        bits.append("Building a sales team with no leader yet — the leadership seat is the gap.")
    if "revops" in fams:
        bits.append("A RevOps hire usually precedes a VP Sales hire by a quarter.")
    if not bits:
        bits.append("On your radar — watch for a second signal before investing time.")
    return " ".join(bits)


def score_jobs(jobs: list[dict]) -> list[dict]:
    """Group a search's jobs by company and score each on the signal families.
    Ranked by score; confluence (2+ core families) earns a bonus."""
    by_company: dict[str, dict] = {}
    for j in jobs:
        company = (j.get("company") or "").strip()
        if not company or company == "—":
            continue
        k = _norm(company)
        if not k:
            continue
        rec = by_company.setdefault(k, {"company": company, "sources": set(), "roles": []})
        rec["sources"].add(j.get("source") or "")
        rec["roles"].append({
            "title": j.get("title") or "", "posted_date": j.get("posted_date"),
            "source": j.get("source"), "text": f"{j.get('title') or ''} {j.get('post_text') or ''}",
        })

    out = []
    for k, rec in by_company.items():
        roles = rec["roles"]
        titles = [r["title"] for r in roles]
        fams: set = set()
        # general families
        if len(roles) >= 3:
            fams.add("multi_role")
        leaders = [t for t in titles if _LEADER.search(t)]
        if leaders:
            fams.add("leadership_open")
        if "linkedin_posts" in rec["sources"]:
            fams.add("founder_post")
        if any(_is_fresh(r["posted_date"]) for r in roles):
            fams.add("fresh")
        if len([s for s in rec["sources"] if s]) >= 2:
            fams.add("multi_source")
        # sales-specific (only when the roles are clearly sales)
        sales_titles = [t for t in titles if _SALES.search(t)]
        if sales_titles:
            junior = [t for t in sales_titles if _JUNIOR_SALES.search(t)]
            has_sales_leader = any(_SALES_LEADER.search(t) for t in sales_titles)
            if junior and not has_sales_leader:
                fams.add("sales_build")
            if any(_REVOPS.search(t) for t in titles):
                fams.add("revops")
        if not fams:
            continue

        base = sum(_FAMILY_WEIGHT[f] for f in fams)
        n_core = len(fams & _CORE)
        conf = 25 if n_core >= 3 else (15 if n_core >= 2 else 0)
        score = min(100, base + conf)
        dates = [r["posted_date"] for r in roles if r.get("posted_date")]
        out.append({
            "company": rec["company"],
            "score": score,
            "n_families": len(fams),
            "confluence": n_core >= 2,
            "families": sorted(fams),
            "signals": [_FAMILY_LABEL[f] for f in sorted(fams)],
            "top_role": (leaders[0] if leaders else (titles[0] if titles else "")),
            "role_count": len(roles),
            "sample_roles": titles[:6],
            "sources": sorted(s for s in rec["sources"] if s),
            "freshest_posted": max(dates) if dates else None,
            "read": _read(fams, leaders[0] if leaders else "", len(roles)),
            "enriched": False,
        })
    out.sort(key=lambda x: (-x["score"], x["company"]))
    return out
