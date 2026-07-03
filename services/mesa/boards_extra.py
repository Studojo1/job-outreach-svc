"""Extra hiring-signal boards for Mesa — India-heavy + remote, all keyless public
endpoints. Same source contract as sources.py:
    scrape(keywords, location, date_posted, workplace_types, experience_levels, max_results)
    -> [{external_id, title, company, location, posted_date, url}]

Sources: Internshala (India interns/freshers, category HTML), Unstop (India jobs
JSON), Himalayas (remote JSON), WorkingNomads (remote JSON).
"""
import html as ihtml
import logging
import re
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
      "Accept": "application/json"}
_T = 25.0


def _days_since(dt: datetime) -> int | None:
    try:
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


# ── Internshala ──────────────────────────────────────────────────────────────
# Keyword search is anon-blocked, so map the query to category slugs. The logged
# out list view carries no post date -> posted_date left blank (open/active).
_INT_MAP = [
    (("devops", "sre", "site reliability", "kubernetes", "docker"), "devops,cloud-computing"),
    (("cloud", "aws", "azure", "gcp"), "cloud-computing,devops"),
    (("python", "django", "flask", "automation", "scripting"), "python-django-development"),
    (("react", "frontend", "front end", "front-end", "angular", "vue", "ui"), "front-end-development,web-development"),
    (("full stack", "fullstack", "mern", "mean", "web"), "web-development,front-end-development,full-stack-development"),
    (("node", "backend", "back end", "back-end", "java", "golang", "php"), "back-end-development,software-development"),
    (("data", "ml", "machine learning", "analyst", "ai"), "data-science,machine-learning"),
    (("android", "ios", "mobile", "flutter"), "mobile-app-development"),
]
_INT_DEFAULT = "software-development,programming"


def _int_cats(keywords: str) -> list[str]:
    k = (keywords or "").lower()
    cats: list[str] = []
    for toks, cat in _INT_MAP:
        if any(t in k for t in toks) and cat not in cats:
            cats.append(cat)
    return cats[:2] or [_INT_DEFAULT]


def _int_company(url: str) -> str:
    m = re.search(r"-at-([a-z0-9-]+?)\d*$", url or "")
    return " ".join(w.capitalize() for w in m.group(1).split("-")) if m else "-"


def scrape_internshala(keywords, location, date_posted="week", workplace_types=None,
                       experience_levels=None, max_results=80):
    wfh = "remote" in [w.lower() for w in (workplace_types or [])]
    out: list[dict] = []
    seen: set = set()
    for cat in _int_cats(keywords):
        path = (f"work-from-home-{cat}" if wfh else cat) + "-internship"
        for page in (1, 2, 3):
            if len(out) >= max_results:
                break
            try:
                r = httpx.get(f"https://internshala.com/internships/{path}/page-{page}/",
                              headers={**_H, "Accept": "text/html"}, timeout=_T, verify=False, follow_redirects=True)
                if r.status_code != 200:
                    break
                txt = r.text
            except Exception as e:  # noqa: BLE001
                logger.warning("[MESA] internshala %s p%s: %s", cat, page, e)
                break
            parts = re.split(r'internshipId="(\d+)"', txt)
            if len(parts) < 3:
                break
            for i in range(1, len(parts), 2):
                cid = parts[i]
                if cid in seen:
                    continue
                seen.add(cid)
                card = parts[i + 1][:4500]
                mt = re.search(r'job-title-href"[^>]*href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>', card)
                if not mt:
                    continue
                href = mt.group(1)
                href = "https://internshala.com" + href if href.startswith("/") else href
                mc = re.search(r'company[-_]name[^>]*>\s*(?:<[^>]+>\s*)?([^<]+?)\s*<', card)
                comp = ihtml.unescape(mc.group(1).strip()) if mc else ""
                if not comp or len(comp) < 2:
                    comp = _int_company(href)
                ml = re.search(r'locations"[^>]*>.*?>\s*([^<]+?)\s*</a>', card, re.S)
                loc = "Work From Home" if "Work From Home" in card[:1600] else (
                    ihtml.unescape(ml.group(1).strip()) if ml else "India")
                out.append({"external_id": f"int_{cid}", "title": ihtml.unescape(mt.group(2).strip()),
                            "company": comp or "-", "location": loc, "posted_date": "", "url": href})
    return out[:max_results]


# ── Unstop (India) — strict recency (its search surfaces evergreen drives) ────────
def scrape_unstop(keywords, location, date_posted="week", workplace_types=None,
                  experience_levels=None, max_results=60):
    q = (keywords or "").replace(" ", "%20")
    out: list[dict] = []
    for page in (1, 2):
        if len(out) >= max_results:
            break
        try:
            rows = httpx.get(
                f"https://unstop.com/api/public/opportunity/search-result?opportunity=jobs&per_page=30&searchTerm={q}&page={page}",
                headers=_H, timeout=_T, verify=False, follow_redirects=True
            ).json().get("data", {}).get("data", []) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("[MESA] unstop %r: %s", keywords, e)
            break
        if not rows:
            break
        for j in rows:
            ua = j.get("updated_at") or ""
            try:
                dt = datetime.fromisoformat(ua.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                dt = None
            if dt is None or _days_since(dt) is None or _days_since(dt) > 21:
                continue
            pu = j.get("public_url") or ""
            url = pu if pu.startswith("http") else "https://unstop.com/" + pu
            out.append({"external_id": f"uns_{j.get('id')}", "title": (j.get("title") or "").strip(),
                        "company": (j.get("organisation") or {}).get("name") or "-",
                        "location": (j.get("region") or "India").title(),
                        "posted_date": dt.date().isoformat(), "url": url})
    return out[:max_results]


# ── Himalayas (remote) ────────────────────────────────────────────────────────
def scrape_himalayas(keywords, location, date_posted="week", workplace_types=None,
                     experience_levels=None, max_results=60):
    out: list[dict] = []
    try:
        jobs = httpx.get(f"https://himalayas.app/jobs/api?limit=50&search={(keywords or '').replace(' ', '+')}",
                         headers=_H, timeout=_T, verify=False, follow_redirects=True).json().get("jobs") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] himalayas %r: %s", keywords, e)
        return out
    for j in jobs:
        try:
            dt = datetime.fromtimestamp(int(j.get("pubDate")), tz=timezone.utc)
        except Exception:
            dt = None
        if dt and _days_since(dt) is not None and _days_since(dt) > 8:
            continue
        out.append({"external_id": "him_" + (j.get("guid") or j.get("applicationLink", "")),
                    "title": (j.get("title") or "").strip(), "company": j.get("companyName") or "-",
                    "location": ", ".join(j.get("locationRestrictions") or []) or "Remote",
                    "posted_date": dt.date().isoformat() if dt else "", "url": j.get("applicationLink") or ""})
    return out[:max_results]


# ── WorkingNomads (remote) — one fetch cached across terms per process ─────────────
_WN_CACHE: dict = {"rows": None}


def scrape_workingnomads(keywords, location, date_posted="week", workplace_types=None,
                         experience_levels=None, max_results=60):
    if _WN_CACHE["rows"] is None:
        try:
            _WN_CACHE["rows"] = httpx.get("https://www.workingnomads.com/api/exposed_jobs/",
                                          headers=_H, timeout=30, verify=False, follow_redirects=True).json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[MESA] workingnomads: %s", e)
            _WN_CACHE["rows"] = []
    toks = [t for t in (keywords or "").lower().split() if len(t) > 2]
    out: list[dict] = []
    for j in _WN_CACHE["rows"]:
        blob = f"{j.get('title', '')} {j.get('tags', '')} {j.get('category_name', '')}".lower()
        if toks and not any(t in blob for t in toks):
            continue
        pd = (j.get("pub_date") or "")[:10]
        try:
            dt = datetime.strptime(pd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
        if dt and _days_since(dt) is not None and _days_since(dt) > 8:
            continue
        out.append({"external_id": "wn_" + (j.get("url") or j.get("title", "")),
                    "title": (j.get("title") or "").strip(), "company": j.get("company_name") or "-",
                    "location": j.get("location") or "Remote", "posted_date": pd, "url": j.get("url") or ""})
        if len(out) >= max_results:
            break
    return out[:max_results]
