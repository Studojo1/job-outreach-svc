"""Contact stage: code-owned waterfall, strongest signal first.

Run-36 shipped a Client Engagement Manager while priya@sherlock.sh sat in
the row's own evidence text, and ignored two post authors who WERE the
hiring contacts. Structurally impossible here: the extraction fields are
consulted in tier order BEFORE any lookup, and LeadsForge candidates are
ranked by hiring AUTHORITY in code (the model never free-picks from a list).

Tiers:
  t0_apply_channel : email/person named in the evidence text
  t1_job_poster    : LinkedIn job page's own poster module
  t2_insider_author: post author who works at the hiring company
  t3_leadsforge    : company+location people search, authority-ranked,
                     employer-verified (fuzzy-name junk discarded)
  t4_web           : one templated web search (budget-gated)
Collisions repair (next candidate), never silently strip. Empty beats
wrong: exhausted waterfall ships blank + needs_contact.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.bob.pipeline import state
from services.bob.textrails import norm_company, norm_person

logger = logging.getLogger(__name__)

_WORKERS = 5
_RANK_A = re.compile(r"recruit|talent|human resources|\bhr\b|people op|people team|staffing|hiring", re.I)
_RANK_B = re.compile(r"founder|co-?founder|\bceo\b|\bcto\b|\bcoo\b|chief|director", re.I)
_RANK_C = re.compile(r"\bhead\b|\blead\b|manager|\bvp\b|vice president", re.I)
# Suffix noise stripped before employer comparison ("Honeywell Technologies" == "Honeywell").
_CO_SUFFIX = re.compile(r"(technologies|technology|solutions|systems|software|services|labs|lab"
                        r"|private|limited|pvt|ltd|inc|llc|india|global|group|ai|io|hq)$")


def _co_root(v: str) -> str:
    n = norm_company(v)
    prev = None
    while n != prev:
        prev, n = n, _CO_SUFFIX.sub("", n)
    return n


def employer_match(person_company: str, company: str, website: str = "") -> bool:
    """Strict at:-check. LeadsForge name search is fuzzy to the point of
    returning identical junk lists for different companies (Kashvi audit:
    2/12 contacts were real), so near-exact root match only."""
    p, c = _co_root(person_company), _co_root(company)
    if not p or not c:
        return False
    if p == c:
        return True
    shorter, longer = sorted((p, c), key=len)
    if longer.startswith(shorter) and len(longer) - len(shorter) <= 4:
        return True
    m = re.search(r"https?://(?:www\.)?([^/]+)", website or "")
    if m:
        droot = _co_root(m.group(1).rsplit(".", 2)[0] if m.group(1).count(".") <= 1
                         else m.group(1).split(".")[-2])
        if droot and droot == p:
            return True
    return False


def rank_people(people: list[dict], company: str, website: str, city: str) -> list[dict]:
    """Employer-verified, authority-ranked candidates. ICs never rank: a Data
    Scientist is who the mandate HIRES, not who hires for it."""
    city_n = (city or "").strip().lower()
    ranked = []
    for p in people or []:
        if not employer_match(p.get("company") or "", company, website):
            continue
        title = p.get("title") or ""
        if _RANK_A.search(title):
            cls = 0
        elif _RANK_B.search(title):
            cls = 1
        elif _RANK_C.search(title):
            cls = 2
        else:
            continue  # individual contributors are never hiring contacts
        city_hit = bool(city_n and city_n in (p.get("city") or "").lower())
        ranked.append((cls, 0 if city_hit else 1, p, city_hit))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [{"name": p["name"], "title": p["title"], "city": p.get("city") or "",
             "tier": "T2" if hit else "T3"} for _, _, p, hit in ranked]


def _job_poster_contact(opp: dict, read_job) -> dict | None:
    if "linkedin.com/jobs/view/" not in (opp.get("evidence_url") or ""):
        return None
    try:
        d = read_job(opp["evidence_url"])
    except Exception:
        return None
    poster = (d or {}).get("poster") or {}
    if not poster.get("name"):
        return None
    title = poster.get("headline") or ""
    if title and not employer_match(title, opp["company"], opp.get("website") or "") \
            and _co_root(title) and "(recruiter)" not in title:
        # headline naming a different org = agency recruiter; usable, labeled.
        title = f"{title} (recruiter)"
    return {"contact_name": poster["name"], "contact_title": title[:150],
            "contact_tier": "T1", "contact_source": "t1_job_poster",
            "contact_profile_url": poster.get("profile_url") or ""}


def waterfall(opp: dict, owners: dict, city: str, fetchers: dict, credits_ok: bool) -> dict:
    """Decide one opportunity's contact. fetchers: read_job, find_people,
    web_people (each injectable for tests). Collision repairs to the next
    tier/candidate instead of stripping to silence."""
    def free(name: str) -> bool:
        pn = norm_person(name)
        owner = owners.get(pn)
        return not (owner and owner[0] != opp["company_norm"])

    # T0: the evidence names the application channel.
    if opp.get("apply_email") or opp.get("apply_person"):
        name = (opp.get("apply_person") or "").strip()
        if name and norm_person(name) == norm_person(opp.get("author_name") or ""):
            title = opp.get("author_headline") or ""
        else:
            title = "named application contact"
        if not name or free(name):
            return {"contact_name": name[:120], "contact_title": (title if name else "")[:150],
                    "contact_tier": "T1", "contact_source": "t0_apply_channel",
                    "contact_email": (opp.get("apply_email") or "")[:200],
                    "contact_profile_url": ""}

    # T1: job page exposes its poster.
    c = _job_poster_contact(opp, fetchers["read_job"])
    if c and free(c["contact_name"]):
        return c

    # T2: the post author is an insider.
    if opp.get("author_affiliation") == "insider" and opp.get("author_name"):
        if free(opp["author_name"]):
            return {"contact_name": opp["author_name"][:120],
                    "contact_title": (opp.get("author_headline") or "")[:150],
                    "contact_tier": "T1", "contact_source": "t2_insider_author",
                    "contact_profile_url": (opp.get("author_profile") or "")[:400]}

    # T3: LeadsForge, authority-ranked, employer-verified.
    try:
        people, _mode = fetchers["find_people"](opp["company"], opp.get("website") or "", city)
    except Exception:
        people = []
    for cand in rank_people(people, opp["company"], opp.get("website") or "", city):
        if free(cand["name"]):
            return {"contact_name": cand["name"][:120], "contact_title": cand["title"][:150],
                    "contact_tier": cand["tier"], "contact_source": "t3_leadsforge",
                    "contact_profile_url": ""}

    # T4: one templated web search, only while credits remain.
    if credits_ok:
        try:
            cand = fetchers["web_people"](opp["company"], city)
        except Exception:
            cand = None
        if cand and cand.get("name") and free(cand["name"]):
            return {"contact_name": cand["name"][:120],
                    "contact_title": (cand.get("title") or "")[:150],
                    "contact_tier": "T3", "contact_source": "t4_web",
                    "contact_profile_url": (cand.get("profile_url") or "")[:400]}

    return {"needs_contact": True}


# ── production fetchers ────────────────────────────────────────────────────────

def _fetch_read_job(url: str) -> dict:
    from services.bob import livecheck
    return livecheck.read_job(url)


def _fetch_find_people(company: str, website: str, city: str):
    from services.bob import leadsforge
    m = re.search(r"https?://(?:www\.)?([^/]+)", website or "")
    return leadsforge.find_people(company=company, domain=m.group(1) if m else "",
                                  locations=[city] if city else [], limit=20)


_IN_TITLE = re.compile(r"^([^\-|–]{2,60})[\-|–]\s*([^|]{2,80})", )


def _fetch_web_people(company: str, city: str) -> dict | None:
    """One credit, strict verification: the result title must name the company."""
    from database.session import SessionLocal
    from services.bob import contextdev_client as ctx
    db = SessionLocal()
    try:
        res = ctx.web_search(
            db, query=f'"{company}" recruiter OR "talent acquisition" {city} site:linkedin.com/in',
            num_results=10,
        )
    finally:
        db.close()
    want = _co_root(company)
    for r in res.get("results") or []:
        url = r.get("url") or ""
        if "linkedin.com/in/" not in url:
            continue
        blob = f'{r.get("title") or ""} {r.get("description") or ""}'
        if want and want not in _co_root(blob) and want not in norm_company(blob):
            continue
        m = _IN_TITLE.match(r.get("title") or "")
        if not m:
            continue
        return {"name": m.group(1).strip(), "title": m.group(2).strip(), "profile_url": url.split("?")[0]}
    return None


def run(db, run_id: int, chat_id: int, params: dict, budget) -> dict:
    opps = state.opportunities(db, run_id, status="scored")
    if not opps:
        return {"in": 0}
    city = (params.get("location") or "").split(",")[0].strip()
    owners = state.table_contact_owners(db, int(params.get("table_id") or 0)) \
        if params.get("table_id") else {}
    fetchers = {"read_job": _fetch_read_job, "find_people": _fetch_find_people,
                "web_people": _fetch_web_people}

    # Network decisions in parallel; owner-map mutation + DB writes serial.
    decisions: dict[int, dict] = {}
    order = sorted(opps, key=lambda o: -(o.get("fit_score") or 0))
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futs = {pool.submit(waterfall, o, dict(owners), city, fetchers,
                            budget.credits_left > 2): o for o in order}
        for fut in as_completed(futs):
            o = futs[fut]
            try:
                decisions[o["id"]] = fut.result()
            except Exception as e:
                logger.exception("[BOB/PIPE] contact waterfall failed for %s", o["company"])
                decisions[o["id"]] = {"needs_contact": True, "_err": str(e)[:120]}

    counts: dict = {}
    for o in order:  # fit order so collisions resolve toward the best rows
        d = decisions.get(o["id"]) or {"needs_contact": True}
        name = d.get("contact_name") or ""
        if name:
            pn = norm_person(name)
            owner = owners.get(pn)
            if owner and owner[0] != o["company_norm"]:
                d = {"needs_contact": True}   # lost the race to a better-fit row
            else:
                owners[pn] = (o["company_norm"], o["company"])
        src = d.get("contact_source") or "none"
        counts[src] = counts.get(src, 0) + 1
        fields = {k: v for k, v in d.items() if not k.startswith("_") and k != "needs_contact"}
        state.transition(db, o["id"], "contacted", needs_contact=bool(d.get("needs_contact")),
                         **fields)
        if d.get("contact_source") == "t4_web":
            budget.spend(1)
    return {"in": len(opps), **counts}
