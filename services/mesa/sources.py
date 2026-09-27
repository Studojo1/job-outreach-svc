"""Extra cookie-free job sources for Mesa — public JSON APIs, no auth, no Apollo.

Aggregated boards that answer "which companies are actively hiring right now and
for what roles". Each scraper returns dicts:
    {external_id, title, company, location, posted_date, url}
The LinkedIn source lives in linkedin_jobs.py; this registry unifies them all
behind one signature: scrape(keywords, location, date_posted, workplace_types,
experience_levels, max_results).
"""

import logging
import urllib.parse

import httpx

from services.mesa.getro import scrape_jobs as _getro_scrape
from services.mesa.linkedin_jobs import scrape_jobs as _linkedin_scrape
from services.mesa.linkedin_posts import scrape_posts as _linkedin_posts_scrape

logger = logging.getLogger(__name__)

_H = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_T = 20.0


def _kw_match(text: str, keywords: str) -> bool:
    toks = [t for t in (keywords or "").lower().split() if len(t) > 2]
    if not toks:
        return True
    low = (text or "").lower()
    return any(t in low for t in toks)


def _get(url: str, headers: dict | None = None, proxy: bool = False):
    """GET with retries. Tries direct first, then (if a proxy is configured)
    falls back through the proxy so a source that's geo/IP-blocked on the
    datacenter IP still resolves. Retries twice per route on 5xx / network
    errors. Raises the last error only if every route fails."""
    from core.config import settings
    proxy_url = (getattr(settings, "LINKEDIN_PROXY_URL", "") or "").strip() or None
    routes: list = [proxy_url] if proxy else [None]
    if not proxy and proxy_url:
        routes.append(proxy_url)  # direct failed -> retry through residential proxy
    last: Exception | httpx.Response | None = None
    for px in routes:
        for _ in range(2):
            try:
                r = httpx.get(url, headers=headers or _H, timeout=_T, verify=False,
                              proxy=px, follow_redirects=True)
                if r.status_code < 500:
                    return r
                last = r
            except Exception as e:  # noqa: BLE001
                last = e
    if isinstance(last, Exception):
        raise last
    if last is not None:
        return last
    raise RuntimeError(f"GET failed with no response: {url}")


def _src_linkedin(keywords, location, date_posted, workplace_types, experience_levels, max_results):
    jobs = _linkedin_scrape(keywords, location, date_posted, workplace_types,
                            experience_levels, max_pages=max(2, min(40, max_results // 10)))
    return [{
        "external_id": j["linkedin_job_id"], "title": j["title"], "company": j["company"],
        "location": j["location"], "posted_date": j.get("posted_date"), "url": j["url"],
    } for j in jobs]


def _src_linkedin_posts(keywords, location, date_posted, workplace_types, experience_levels, max_results):
    # Authenticated feed-post search (founder/recruiter hiring posts). Already
    # returns the standard {external_id,title,company,location,posted_date,url} shape.
    return _linkedin_posts_scrape(keywords, location, date_posted, workplace_types,
                                  experience_levels, max_results)


def _src_getro(keywords, location, date_posted, workplace_types, experience_levels, max_results):
    # VC-portfolio boards (Accel, Blume) via Getro's public JSON API — funded
    # startups hiring right now. Already returns the standard job shape.
    return _getro_scrape(keywords, location, date_posted, workplace_types, experience_levels, max_results)


def _src_remotive(keywords, location, *_):
    url = "https://remotive.com/api/remote-jobs?limit=80"
    if keywords:
        url += "&search=" + urllib.parse.quote(keywords)
    out = []
    try:
        for j in _get(url).json().get("jobs", []):
            out.append({
                "external_id": str(j.get("id")), "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location") or "Remote",
                "posted_date": (j.get("publication_date") or "")[:10], "url": j.get("url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] remotive failed: %s", e)
    return out


def _src_remoteok(keywords, location, *_):
    out = []
    try:
        data = _get("https://remoteok.com/api").json()
        rows = data[1:] if (data and isinstance(data[0], dict) and "legal" in str(data[0]).lower()) else data
        for j in rows:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            text = f"{j.get('position', '')} {' '.join(j.get('tags', []) or [])}"
            if not _kw_match(text, keywords):
                continue
            out.append({
                "external_id": str(j.get("id")), "title": j.get("position", ""),
                "company": j.get("company", ""), "location": j.get("location") or "Remote",
                "posted_date": (j.get("date") or "")[:10], "url": j.get("url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] remoteok failed: %s", e)
    return out


def _src_arbeitnow(keywords, location, *_):
    out = []
    try:
        for j in _get("https://www.arbeitnow.com/api/job-board-api").json().get("data", []):
            text = f"{j.get('title', '')} {' '.join(j.get('tags', []) or [])}"
            if not _kw_match(text, keywords):
                continue
            if location and location.lower() not in (j.get("location") or "").lower() and not j.get("remote"):
                continue
            loc = (j.get("location") or "") + (" · Remote" if j.get("remote") else "")
            out.append({
                "external_id": j.get("slug") or str(j.get("url")), "title": j.get("title", ""),
                "company": j.get("company_name", ""), "location": loc.strip(" ·"),
                "posted_date": "", "url": j.get("url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] arbeitnow failed: %s", e)
    return out


_MUSE_CATS = [
    "Marketing", "Sales", "Software Engineering", "Data Science", "Design and UX",
    "Product Management", "Project Management", "Finance", "HR & Recruiting",
    "Operations", "Customer Service", "Business & Strategy", "Account Management",
]


def _src_themuse(keywords, location, *_):
    kw = (keywords or "").lower()
    chosen = [c for c in _MUSE_CATS if any(w in kw for w in c.lower().replace("&", " ").split())]
    if not chosen:
        chosen = ["Marketing", "Software Engineering", "Sales", "Data Science"]
    seen = {}
    try:
        for cat in chosen[:3]:
            url = f"https://www.themuse.com/api/public/jobs?page=0&descending=true&category={urllib.parse.quote(cat)}"
            if location:
                url += "&location=" + urllib.parse.quote(location)
            for j in _get(url).json().get("results", []):
                if keywords and not _kw_match(j.get("name", ""), keywords):
                    continue
                eid = str(j.get("id"))
                if not eid or eid in seen:
                    continue
                locs = ", ".join(l.get("name", "") for l in (j.get("locations") or [])[:2])
                seen[eid] = {
                    "external_id": eid, "title": j.get("name", ""),
                    "company": (j.get("company") or {}).get("name", ""),
                    "location": locs or "Flexible",
                    "posted_date": (j.get("publication_date") or "")[:10],
                    "url": (j.get("refs") or {}).get("landing_page", ""),
                }
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] themuse failed: %s", e)
    return list(seen.values())


def _src_instahyre(keywords, location, *_):
    out = []
    url = "https://www.instahyre.com/api/v1/job_search/?job_type=0"
    if keywords:
        url += "&q=" + urllib.parse.quote(keywords)
    try:
        h = {**_H, "Referer": "https://www.instahyre.com/search-jobs/"}
        for o in _get(url, headers=h).json().get("objects", []):
            title = o.get("title", "")
            if not _kw_match(title, keywords):
                continue
            emp = o.get("employer")
            company = ""
            if isinstance(emp, dict):
                company = emp.get("company_name") or emp.get("name") or ""
            locs = o.get("locations")
            parts = []
            if isinstance(locs, list):
                for x in locs:
                    name = x if isinstance(x, str) else (x.get("name", "") if isinstance(x, dict) else "")
                    if name and not name.startswith("/"):  # skip resource URIs
                        parts.append(name)
            loc = ", ".join(parts)
            pub = o.get("public_url", "") or ""
            # InstaHyre denormalises company/city into the slug: ...-at-{company}-{city}/
            if not company and "-at-" in pub:
                company = pub.rstrip("/").split("-at-")[-1].replace("-", " ").title()
            out.append({
                "external_id": str(o.get("id")), "title": title, "company": company or "—",
                "location": loc or "India", "posted_date": None,
                "url": ("https://www.instahyre.com" + pub) if pub.startswith("/") else pub,
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] instahyre failed: %s", e)
    return out


def _src_jobicy(keywords, location, *_):
    url = "https://jobicy.com/api/v2/remote-jobs?count=50"
    toks = (keywords or "").split()
    if toks:
        url += "&tag=" + urllib.parse.quote(toks[0])  # server-side tag filter
    out = []
    try:
        for j in _get(url).json().get("jobs", []):
            out.append({
                "external_id": str(j.get("id")), "title": j.get("jobTitle", ""),
                "company": j.get("companyName", ""), "location": j.get("jobGeo") or "Remote",
                "posted_date": (j.get("pubDate") or "")[:10], "url": j.get("url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] jobicy failed: %s", e)
    return out


def _src_weworkremotely(keywords, location, *_):
    import html
    import re
    out = []
    try:
        rss = _get("https://weworkremotely.com/remote-jobs.rss", headers={**_H, "Accept": "application/rss+xml"}).text
        for m in re.finditer(r"<item>(.*?)</item>", rss, re.S):
            blk = m.group(1)

            def tag(t):
                mm = re.search(rf"<{t}>(.*?)</{t}>", blk, re.S)
                return html.unescape(mm.group(1).strip()) if mm else ""
            title = tag("title")
            if not _kw_match(title, keywords):
                continue
            link = tag("link")
            company, role = (title.split(":", 1) + [""])[:2] if ":" in title else ("", title)
            out.append({
                "external_id": link or title, "title": (role or title).strip(),
                "company": company.strip(), "location": tag("region") or "Remote",
                "posted_date": tag("pubDate")[:16], "url": link,
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] weworkremotely failed: %s", e)
    return out


def _src_indeed(keywords, location, *_):
    """Best-effort: Indeed is Cloudflare-protected and usually returns 403 from
    datacenter/residential IPs. Parses the embedded job-card JSON when it does
    get through; returns [] (gracefully) when blocked."""
    import json as _json
    import re
    dom = "in.indeed.com" if (location and any(c in location.lower() for c in
          ("india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune", "chennai", "gurgaon", "noida"))) else "www.indeed.com"
    url = f"https://{dom}/jobs?q={urllib.parse.quote(keywords or '')}&sort=date"
    if location:
        url += "&l=" + urllib.parse.quote(location)
    out = []
    try:
        r = _get(url, headers={**_H, "Accept": "text/html"}, proxy=True)
        if r.status_code != 200:
            logger.info("[MESA] indeed blocked (HTTP %d)", r.status_code)
            return []
        m = re.search(r'mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});', r.text)
        if not m:
            return []
        results = (_json.loads(m.group(1)).get("metaData", {})
                   .get("mosaicProviderJobCardsModel", {}).get("results", []))
        for j in results:
            jk = j.get("jobkey")
            if not jk:
                continue
            out.append({
                "external_id": jk, "title": j.get("title", ""), "company": j.get("company", ""),
                "location": j.get("formattedLocation", ""), "posted_date": None,
                "url": f"https://{dom}/viewjob?jk={jk}",
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] indeed failed: %s", e)
    return out


def _src_naukri(keywords, location, *_):
    """Best-effort: Naukri's job API requires reCAPTCHA (HTTP 406) for bot
    traffic; returns [] gracefully when gated."""
    url = ("https://www.naukri.com/jobapi/v3/search?noOfResults=20&urlType=search_by_keyword"
           f"&searchType=adv&keyword={urllib.parse.quote(keywords or '')}&pageNo=1")
    if location:
        url += "&location=" + urllib.parse.quote(location)
    h = {**_H, "appid": "109", "systemid": "Naukri", "clientid": "d3skt0p", "Referer": "https://www.naukri.com/"}
    out = []
    try:
        r = _get(url, headers=h, proxy=True)
        if r.status_code != 200:
            logger.info("[MESA] naukri blocked (HTTP %d)", r.status_code)
            return []
        for j in r.json().get("jobDetails", []):
            ph = j.get("placeholders") or []
            loc = next((p.get("label", "") for p in ph if p.get("type") == "location"), "")
            out.append({
                "external_id": str(j.get("jobId")), "title": j.get("title", ""),
                "company": j.get("companyName", ""), "location": loc,
                "posted_date": j.get("footerPlaceholderLabel", ""),
                "url": "https://www.naukri.com" + (j.get("jdURL", "") or ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] naukri failed: %s", e)
    return out


# ── Direct ATS boards (Greenhouse / Lever / Ashby) ──────────────────────────
# The source of truth for most startups — fresher and richer than aggregators,
# no auth needed. These are per-company endpoints, so we iterate a curated board
# list and keyword-filter. Extend/override via env MESA_ATS_BOARDS as a
# comma-list of "provider:token" (e.g. "greenhouse:stripe,ashby:ramp").
_ATS_DEFAULT: list[tuple[str, str]] = [
    ("greenhouse", "stripe"), ("greenhouse", "databricks"), ("greenhouse", "gitlab"),
    ("greenhouse", "cloudflare"), ("greenhouse", "coinbase"), ("greenhouse", "robinhood"),
    ("greenhouse", "dropbox"), ("greenhouse", "benchling"), ("greenhouse", "gusto"),
    ("greenhouse", "samsara"), ("greenhouse", "instacart"), ("greenhouse", "doordash"),
    ("greenhouse", "brex"), ("greenhouse", "plaid"), ("greenhouse", "affirm"),
    ("ashby", "ramp"), ("ashby", "linear"), ("ashby", "vanta"), ("ashby", "mercury"),
    ("ashby", "posthog"), ("ashby", "hex"), ("ashby", "watershed"),
]
# Lever handles vary per company (often 404); the provider path stays supported
# for boards added explicitly via MESA_ATS_BOARDS (e.g. "lever:yourcompany").


def _ats_boards() -> list[tuple[str, str]]:
    from core.config import settings
    raw = (getattr(settings, "MESA_ATS_BOARDS", "") or "").strip()
    if raw:
        out = []
        for chunk in raw.split(","):
            if ":" in chunk:
                prov, tok = chunk.split(":", 1)
                out.append((prov.strip().lower(), tok.strip()))
        return out or _ATS_DEFAULT
    return _ATS_DEFAULT


def _src_ats(keywords, location, date_posted, workplace_types, experience_levels, max_results):
    """Scrape live jobs straight off company ATS boards, keyword-filtered."""
    boards = _ats_boards()
    cap = max_results or 60
    per_board = max(2, cap // max(1, len(boards)))
    out: list[dict] = []
    loc_kw = (location or "").split(",")[0].strip().lower()
    for provider, token in boards:
        if len(out) >= cap:
            break
        got = 0
        try:
            if provider == "greenhouse":
                rows = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs").json().get("jobs", [])
                items = [(r.get("title", ""), (r.get("location") or {}).get("name", ""),
                          str(r.get("id")), r.get("absolute_url", ""), (r.get("updated_at") or "")[:10]) for r in rows]
            elif provider == "lever":
                rows = _get(f"https://api.lever.co/v0/postings/{token}?mode=json").json()
                items = [(r.get("text", ""), (r.get("categories") or {}).get("location", ""),
                          str(r.get("id")), r.get("hostedUrl", ""), "") for r in rows]
            elif provider == "ashby":
                rows = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}").json().get("jobs", [])
                items = [(r.get("title", ""), r.get("location", ""), str(r.get("id")),
                          r.get("jobUrl", ""), (r.get("publishedAt") or "")[:10]) for r in rows]
            else:
                continue
        except Exception as e:  # noqa: BLE001
            logger.info("[MESA] ats %s/%s failed: %s", provider, token, e)
            continue
        company = token.replace("-", " ").title()
        for title, loc, jid, url, posted in items:
            if got >= per_board or len(out) >= cap:
                break
            if not _kw_match(title, keywords):
                continue
            if loc_kw and loc and loc_kw not in loc.lower() and "remote" not in loc.lower():
                continue
            out.append({
                "external_id": f"{provider}:{token}:{jid}", "title": title,
                "company": company, "location": loc or "—", "posted_date": posted or None,
                "url": url,
            })
            got += 1
    logger.info("[MESA] ats -> %d jobs across %d boards", len(out), len(boards))
    return out


def _src_himalayas(keywords, location, *_):
    """Himalayas remote-jobs API (no key)."""
    out: list[dict] = []
    try:
        data = _get("https://himalayas.app/jobs/api?limit=100").json()
        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not _kw_match(f"{title} {j.get('excerpt', '')}", keywords):
                continue
            locs = ", ".join(j.get("locationRestrictions", []) or []) or "Remote"
            out.append({
                "external_id": str(j.get("guid") or j.get("applicationLink")),
                "title": title, "company": j.get("companyName", ""),
                "location": locs, "posted_date": None,
                "url": j.get("applicationLink") or j.get("guid", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] himalayas failed: %s", e)
    return out


def _src_adzuna(keywords, location, *_):
    """Adzuna aggregated jobs API. Key-gated: no-op unless ADZUNA_APP_ID/KEY set."""
    from core.config import settings
    app_id = (getattr(settings, "ADZUNA_APP_ID", "") or "").strip()
    app_key = (getattr(settings, "ADZUNA_APP_KEY", "") or "").strip()
    if not (app_id and app_key):
        return []
    country = (getattr(settings, "ADZUNA_COUNTRY", "") or "in").strip().lower()
    out: list[dict] = []
    try:
        url = (f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
               f"?app_id={app_id}&app_key={app_key}&results_per_page=50&content-type=application/json"
               f"&what={urllib.parse.quote(keywords or '')}")
        if location:
            url += "&where=" + urllib.parse.quote(location)
        for j in _get(url).json().get("results", []):
            out.append({
                "external_id": str(j.get("id")), "title": j.get("title", ""),
                "company": (j.get("company") or {}).get("display_name", ""),
                "location": (j.get("location") or {}).get("display_name", ""),
                "posted_date": (j.get("created") or "")[:10], "url": j.get("redirect_url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] adzuna failed: %s", e)
    return out


# Registry — every value has the same signature.
SOURCE_SCRAPERS = {
    "linkedin": _src_linkedin,
    "linkedin_posts": _src_linkedin_posts,
    "getro": _src_getro,
    "themuse": _src_themuse,
    "remotive": _src_remotive,
    "remoteok": _src_remoteok,
    "arbeitnow": _src_arbeitnow,
    "instahyre": _src_instahyre,
    "jobicy": _src_jobicy,
    "weworkremotely": _src_weworkremotely,
    "indeed": _src_indeed,    # best-effort — often Cloudflare-blocked
    "naukri": _src_naukri,    # best-effort — often reCAPTCHA-gated
    "ats": _src_ats,          # direct Greenhouse/Lever/Ashby boards (source of truth)
    "himalayas": _src_himalayas,
    "adzuna": _src_adzuna,    # key-gated (ADZUNA_APP_ID/KEY)
}
ALL_SOURCES = list(SOURCE_SCRAPERS.keys())
# Sources that reliably return keyword-relevant data with no auth / captcha / paid API.
RELIABLE_SOURCES = ["linkedin", "linkedin_posts", "getro", "ats", "themuse", "remotive", "remoteok", "arbeitnow", "jobicy", "himalayas", "weworkremotely"]
# Beta: auth-/bot-walled or no real keyword search — kept for when an aggregator
# key (e.g. Adzuna / SerpApi) is wired. instahyre's public API ignores the query;
# indeed is Cloudflare-blocked; naukri requires reCAPTCHA; adzuna needs a key.
BETA_SOURCES = ["instahyre", "indeed", "naukri", "adzuna"]
# Defaults for a new search. ATS gives direct-from-company roles up front.
DEFAULT_SOURCES = ["linkedin", "ats", "getro", "themuse", "remotive", "jobicy"]
