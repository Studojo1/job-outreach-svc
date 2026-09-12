"""Naukri via a rendered browser page — fallback for the reCAPTCHA-gated job API.

Naukri's JSON API returns 406 for bot traffic, but the rendered site serves job
tuples to a real browser fingerprint (verified against live Naukri, Jul 2026).
Drives headless Chromium through the residential proxy (LINKEDIN_PROXY_URL) and
extracts the server-rendered `.srp-jobtuple-wrapper` cards — title, company,
location, posted-ago, and the canonical job URL.

Freshness comes from Naukri's own jobAge filter plus the per-card posted label.
Returns the standard Mesa source shape; [] on any failure — never raises.
"""

import logging
import re
from urllib.parse import quote

from core.config import settings

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
_LAUNCH_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", "--window-size=1280,900"]
# UI date key -> Naukri jobAge (days)
_JOB_AGE = {"24h": 1, "week": 7, "month": 30, "any": 0}

_EXTRACT = r"""() => {
  const out=[];
  document.querySelectorAll('.srp-jobtuple-wrapper, article.jobTuple').forEach(n=>{
    const a=n.querySelector('a.title, a.jobTitle, h2 a');
    if(!a) return;
    out.push({
      title:(a.innerText||'').trim(),
      url:a.href.split('?')[0],
      company:((n.querySelector('.comp-name, a.subTitle, .companyInfo a')||{}).innerText||'').trim(),
      location:((n.querySelector('.locWdth, .loc span, .location')||{}).innerText||'').trim(),
      posted:((n.querySelector('.job-post-day, .fleft.postedDate')||{}).innerText||'').trim(),
    });
  });
  return out; }"""


def _proxy() -> dict | None:
    raw = (getattr(settings, "LINKEDIN_PROXY_URL", "") or "").strip()
    if not raw:
        return None
    m = re.match(r"(https?|socks5)://(?:([^:@/]+):([^@/]+)@)?([^:/]+):(\d+)", raw)
    if not m:
        return None
    scheme, user, pwd, host, port = m.groups()
    proxy: dict = {"server": f"{scheme}://{host}:{port}"}
    if user:
        proxy["username"], proxy["password"] = user, pwd
    return proxy


def _posted_days(label: str) -> int | None:
    low = (label or "").lower()
    if any(k in low for k in ("just now", "today", "few hours")):
        return 0
    m = re.search(r"(\d+)\s*day", low)
    if m:
        return int(m.group(1))
    return 31 if "30+" in low else None


def scrape_naukri_rendered(keywords: str, location: str = "", date_posted: str = "week",
                           workplace_types=None, experience_levels=None,
                           max_results: int = 40) -> list[dict]:
    """Mesa-source signature. Renders one Naukri search page per call."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        logger.error("[MESA_NAUKRI] playwright unavailable: %s", e)
        return []
    slug = re.sub(r"[^a-z0-9]+", "-", (keywords or "jobs").lower()).strip("-")
    loc = re.sub(r"[^a-z0-9]+", "-", (location or "india").lower().split(",")[0]).strip("-")
    age = _JOB_AGE.get(date_posted, 7)
    url = (f"https://www.naukri.com/{slug}-jobs-in-{loc}"
           + (f"?jobAge={age}" if age else ""))
    rows: list[dict] = []
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True, proxy=_proxy(), args=_LAUNCH_ARGS,
                                    ignore_default_args=["--enable-automation"])
            ctx = br.new_context(user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="en-US")
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(".srp-jobtuple-wrapper, article.jobTuple", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(2000)
            for _ in range(3):
                page.mouse.wheel(0, 2800)
                page.wait_for_timeout(800)
            cards = page.evaluate(_EXTRACT)
            br.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA_NAUKRI] render failed for %r: %s", keywords, e)
        return []
    for c in cards[:max_results]:
        if not c.get("title") or not c.get("url"):
            continue
        days = _posted_days(c.get("posted", ""))
        if days is not None and age and days > age:
            continue  # Naukri sometimes leaks older tuples past its own filter
        rows.append({
            "external_id": "naukri:" + c["url"].rstrip("/").rsplit("-", 1)[-1],
            "title": c["title"],
            "company": c.get("company", ""),
            "location": c.get("location", "") or location or "India",
            "posted_date": c.get("posted") or None,
            "url": c["url"],
        })
    logger.info("[MESA_NAUKRI] %r -> %d rendered jobs", keywords, len(rows))
    return rows
