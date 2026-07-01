"""Authenticated LinkedIn *feed post* scraping for Mesa — finds hiring posts that
founders/recruiters publish on the feed (not in the Jobs board), with the apply
link/email pulled from the post body.

Unlike linkedin_jobs.py (cookie-free guest Jobs API), the content/post search is
auth-gated, so this drives a real Chromium session (Playwright) through the same
residential proxy (LINKEDIN_PROXY_URL) using a dedicated **Mesa burner** session
(MESA_LI_AT / MESA_JSESSIONID) — completely separate from the linkedin_outreach
product's per-user tokens (which this module never reads or touches).

LinkedIn fully obfuscates DOM classes, so extraction keys off the stable
`[componentkey]` node + text parsing. The content-search URL already filters by
date, so freshness is guaranteed by the query, not by per-post URNs.

Returns the standard Mesa source dict shape so it flows through runner.py/storage
unchanged, plus post-specific extras (author/apply_link/post_text) persisted by
migration 040:
    {external_id, title, company, location, posted_date, url,
     author, apply_link, post_text}
"""

import hashlib
import logging
import re
from urllib.parse import quote

from core.config import settings

logger = logging.getLogger(__name__)

# UI date key -> LinkedIn content-search datePosted token
DATE_POSTED = {"24h": "past-24h", "week": "past-week", "month": "past-month", "any": ""}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
_LAUNCH_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", "--window-size=1280,900"]
_STICKY = "mesaposts"  # pin one Evomi residential IP for the session

# ── extraction dictionaries ───────────────────────────────────────────────────
_CHROME = re.compile(
    r"^(feed post|follow|following|•|free|promoted|see more|…more|\.\.\.more|like|comment|comments|"
    r"repost|reposts|send|react|reactions?|activate to view larger image|and \d+ others|view profile|"
    r"connect|message|\d[\d,]*\s*(likes?|comments?|reposts?)|loaded|show more results|"
    r"\d+(st|nd|rd|th)?\+?)\s*$", re.I)
_DEGREE = re.compile(r"^(1st|2nd|3rd\+?|•)$", re.I)
_APPLY_HOSTS = ("lnkd.in", "forms.gle", "forms.office", "docs.google", "typeform", "tally.so",
                "notion.so", "notion.site", "lever.co", "greenhouse.io", "ashbyhq", "workable",
                "zoho.", "airtable", "bit.ly", "cutt.ly", "rb.gy", "careers", "jobs.", "apply.",
                "wellfound", "instahyre", "angel.co")
_HIRING_POS = ("hiring", "we're hiring", "were hiring", "we are hiring", "looking for",
               "join our team", "join us", "open role", "open position", "opening", "openings",
               "apply", "vacancy", "recruiting", "internship", "intern ", "we need", "now hiring",
               "dm ", "drop your", "share your resume", "send your cv", "urgently hiring", "walk-in")
_HIRING_NEG = ("how to get", "tips to get", "before you ask", "here's how i", "story of how",
               "i got placed", "got my internship", "my journey", "a guide to", "enroll now",
               "cohort is live", "webinar")


def _proxy_playwright() -> dict | None:
    """Parse LINKEDIN_PROXY_URL into a Playwright proxy dict with an Evomi sticky
    session appended (copied convention, not imported from linkedin_outreach)."""
    url = (getattr(settings, "LINKEDIN_PROXY_URL", "") or "").strip()
    if not url:
        return None
    m = re.match(r"^(http://[^:]+):([^@]+)@(.+)$", url)
    if m and "_session-" not in m.group(2):
        url = f"{m.group(1)}:{m.group(2)}_session-{_STICKY}@{m.group(3)}"
    m2 = re.match(r"^https?://([^:]+):([^@]+)@(.+)$", url)
    if m2:
        return {"server": f"http://{m2.group(3)}", "username": m2.group(1), "password": m2.group(2)}
    return {"server": url}


def _burner_cookies() -> list[dict] | None:
    """Mesa burner session cookies from settings (never the outreach tokens)."""
    li_at = (getattr(settings, "MESA_LI_AT", "") or "").strip()
    if not li_at:
        return None
    jsess = (getattr(settings, "MESA_JSESSIONID", "") or "").strip()
    cookies = [{"name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/",
                "httpOnly": True, "secure": True, "sameSite": "None"}]
    if jsess:
        cookies.append({"name": "JSESSIONID", "value": jsess if jsess.startswith('"') else f'"{jsess}"',
                        "domain": ".linkedin.com", "path": "/", "httpOnly": False,
                        "secure": True, "sameSite": "None"})
    return cookies


# ── text parsing ──────────────────────────────────────────────────────────────
# LinkedIn renders a post card's innerText as, roughly:
#   "Feed post <Author> • <degree> <Author headline> <Nm> • Follow <POST BODY>
#    Activate to view... N likes N comments"
# The pieces are separated inconsistently (newlines OR inline bullets), so we
# parse structurally off the fixed markers instead of guessing line positions.
_TIME_RE = re.compile(r"\b(\d+)\s*(m|h|d|w|mo)\b\s*[•·]")
_TRAILING_RE = re.compile(r"\b\d[\d,]*\s*(?:likes?|comments?|reposts?|impressions?)\b", re.I)


def _split_card(text: str) -> tuple[str, str, str]:
    """Return (author, posted, body) from a post card's innerText."""
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"^\s*feed post\s*", "", t, flags=re.I)
    # author = the run of text before the first bullet/degree marker
    am = re.match(r"([A-Za-z][\w.'’\-&() ]{1,55}?)\s*(?:[•·]|\b(?:1st|2nd|3rd\+?)\b)", t)
    author = am.group(1).strip() if am else ""
    author = re.sub(r"\s*(?:feed post)$", "", author, flags=re.I).strip()
    tm = _TIME_RE.search(t)
    posted = f"{tm.group(1)}{tm.group(2)}" if tm else ""
    # body = everything after "Follow" (post content starts after the follow btn);
    # fall back to text after the time marker.
    fm = re.search(r"\bFollow\b\s*(.+)$", t, flags=re.I)
    body = fm.group(1) if fm else (t[tm.end():] if tm else t)
    body = _TRAILING_RE.split(body)[0]
    body = re.sub(r"\s*(?:…more|\.\.\.more|see more)\s*$", "", body, flags=re.I).strip()
    return author, posted, body


def _parse(text: str, links: list[str]) -> dict | None:
    author, posted, body = _split_card(text)
    low = body.lower()
    if not (any(k in low for k in _HIRING_POS) and not any(k in low for k in _HIRING_NEG)):
        return None  # drop non-hiring / advice posts
    emails = list(dict.fromkeys(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", body)))
    apply = []
    for l in links:
        ll = l.lower()
        if "linkedin.com" in ll and "lnkd.in" not in ll:
            continue
        if any(h in ll for h in _APPLY_HOSTS):
            apply.append(l.split("?")[0])
    apply = list(dict.fromkeys(apply))
    if apply:
        apply_link = apply[0]
    elif emails:
        apply_link = f"mailto:{emails[0]}"
    else:
        apply_link = ""
    # role — the job title being hired for
    role = ""
    rm = re.search(r"(?:hiring|we're hiring|were hiring|we are hiring|looking for|role|position)\b"
                   r"\s*[:\-–—]?\s*(?:a|an|for)?\s*"
                   r"([A-Z][A-Za-z0-9/&\+\. ]{2,45}?(?:intern|internship|manager|engineer|developer|"
                   r"designer|analyst|associate|executive|lead|marketer|specialist|coordinator|"
                   r"officer|consultant|strategist|writer|creator|sde|swe))",
                   body, re.I)
    if rm:
        role = re.sub(r"\s+", " ", rm.group(1)).strip(" .-–—")
    # company — prefer an explicit "<Company> is hiring" / "@Company"; else fall back to author
    comp = ""
    cm = (re.search(r"\b([A-Z][A-Za-z0-9&.\- ]{1,34}?)\s+is\s+(?:hiring|looking)", body)
          or re.search(r"(?:join|at)\s+([A-Z][A-Za-z0-9&.\-]{2,30})\b", body)
          or re.search(r"@\s*([A-Za-z][A-Za-z0-9&.\-]{2,30})", body))
    if cm:
        comp = cm.group(1).strip()
    external_id = "post_" + hashlib.sha1(f"{author}|{body[:200]}".encode()).hexdigest()[:20]
    return {
        "external_id": external_id,
        "title": role or "Hiring post",
        "company": comp or author or "",
        "location": "",
        "posted_date": posted,
        "url": apply_link or "",
        "author": author,
        "apply_link": apply_link or "",
        "post_text": body[:1500],
    }


# ── public entry point (Mesa source signature) ────────────────────────────────
def scrape_posts(keywords: str, location: str = "", date_posted: str = "24h",
                 workplace_types=None, experience_levels=None, max_results: int = 150) -> list[dict]:
    """Search LinkedIn feed posts for hiring posts matching `keywords`.

    Signature matches the other Mesa sources so it registers in SOURCE_SCRAPERS
    and flows through runner.py unchanged. `location` is appended to the query
    (content search has no separate geo filter); workplace/experience are ignored
    (post concepts don't map). Returns [] on any failure — never raises.
    """
    if not _burner_cookies():
        logger.warning("[MESA_POSTS] no MESA_LI_AT configured — skipping posts source")
        return []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        logger.error("[MESA_POSTS] playwright unavailable: %s", e)
        return []

    dp = DATE_POSTED.get(date_posted, "past-24h")
    # Search the keyword only. Do NOT append `location` to the query — content
    # search has no geo filter, so appending it forces posts to literally contain
    # the location word ("...India"), which almost no hiring post does and which
    # zeroed out real results. LinkedIn personalises to the burner's region anyway.
    # No sortBy => LinkedIn's default 'top match' (relevance) surfaces hiring posts
    # across the whole date window; forcing date-sort buries relevant older posts
    # under fresh non-hiring noise (past the scroll limit) on broad keywords.
    q = keywords.strip()
    url = (f"https://www.linkedin.com/search/results/content/?keywords={quote(q)}"
           + (f'&datePosted=%22{dp}%22' if dp else ""))

    rows: list[dict] = []
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True, proxy=_proxy_playwright(),
                                    args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"])
            ctx = br.new_context(user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="en-US")
            ctx.add_cookies(_burner_cookies())
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            if any(x in page.url for x in ("login", "authwall", "checkpoint")):
                logger.error("[MESA_POSTS] session invalid (redirected to %s) — burner needs re-auth", page.url)
                br.close()
                return []
            scrolls = max(4, min(14, max_results // 8))
            for _ in range(scrolls):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1500)
            raw = page.evaluate(
                r"""() => {
                  const out=[];
                  document.querySelectorAll('[componentkey]').forEach(n=>{
                    const text=(n.innerText||'').trim();
                    if(text.length<140 || text.length>8000) return;
                    if(!/(\b\d+\s*(m|h|d|w|mo|min|hour|day|week)\b\s*[•·])|(•\s*\d)/i.test(text)) return;
                    const links=Array.from(n.querySelectorAll('a[href]')).map(a=>a.href);
                    out.push({text:text.slice(0,2500), links});
                  });
                  return out;
                }""")
            br.close()
    except Exception as e:  # noqa: BLE001
        logger.error("[MESA_POSTS] scrape failed for %r: %s", keywords, e)
        return []

    # dedupe nested componentkey nodes (keep outermost/longest), then parse
    raw.sort(key=lambda p: -len(p["text"]))
    kept: list[dict] = []
    for p in raw:
        if any(p["text"][:120] in k["text"] for k in kept):
            continue
        kept.append(p)
    for p in kept:
        parsed = _parse(p["text"], p["links"])
        if parsed:
            rows.append(parsed)
    logger.info("[MESA_POSTS] %r -> %d hiring posts", keywords, len(rows))
    return rows
