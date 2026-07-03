"""Community hiring signals for Mesa — where founders/teams post openings in
prose, not on a board.

- Hacker News "Ask HN: Who is hiring?" — the monthly mega-thread (hundreds of
  pipe-delimited hiring comments), via the free Algolia API.
- Reddit r/forhire + r/hiring "[Hiring]" posts, via the residential proxy
  (datacenter IPs get 403'd by reddit).

Same source contract as sources.py:
    scrape(keywords, location, date_posted, workplace_types, experience_levels, max_results)
    -> [{external_id, title, company, location, posted_date, url, post_text}]
"""
import html as ihtml
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
      "Accept": "application/json"}
_T = 25.0
_ROLE_RE = re.compile(r"\b(engineer|developer|scientist|designer|manager|intern|analyst|"
                      r"devops|sre|architect|lead|marketer|founding|full[- ]?stack|frontend|"
                      r"backend|data|ml|ai|product|qa|sdet|support|recruiter|growth)\b", re.I)
_REMOTE_RE = re.compile(r"\b(remote|onsite|on-site|hybrid|wfh)\b", re.I)


def _toks(keywords: str) -> list[str]:
    return [t for t in (keywords or "").lower().split() if len(t) > 2]


def _clean(htmltext: str) -> tuple[str, str]:
    """Return (plaintext, first_apply_url) from an HN comment's HTML."""
    m = re.search(r'href="([^"]+)"', htmltext or "")
    url = ihtml.unescape(m.group(1)) if m else ""
    txt = re.sub(r"<[^>]+>", " ", htmltext or "")
    txt = ihtml.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt, url


# ── Hacker News: Who is hiring ────────────────────────────────────────────────
def scrape_hackernews(keywords, location, date_posted="month", workplace_types=None,
                      experience_levels=None, max_results=120):
    toks = _toks(keywords)
    out: list[dict] = []
    try:
        stories = httpx.get(
            "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring"
            "&query=who%20is%20hiring&hitsPerPage=5", headers=_H, timeout=_T, verify=False
        ).json().get("hits", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] hn story lookup: %s", e)
        return out
    story = next((s for s in stories if "who is hiring" in (s.get("title") or "").lower()), None)
    if not story:
        return out
    sid = story["objectID"]
    month = (story.get("title") or "").split("(")[-1].rstrip(")")
    try:
        item = httpx.get(f"https://hn.algolia.com/api/v1/items/{sid}", headers=_H, timeout=30, verify=False).json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] hn item %s: %s", sid, e)
        return out
    for c in item.get("children", []):
        raw = c.get("text")
        if not raw:
            continue
        txt, url = _clean(raw)
        if not _match_any(txt, toks):
            continue
        first = txt.split(" | ")[0][:80] if " | " in txt else txt[:80]
        segs = [s.strip() for s in txt.split("|")]
        company = segs[0][:60] if segs else first
        role = next((s for s in segs if _ROLE_RE.search(s)), "")[:80]
        rem = _REMOTE_RE.search(txt)
        out.append({
            "external_id": f"hn_{c.get('id')}", "title": (role or first).strip(),
            "company": company or "-", "location": (rem.group(1).title() if rem else "See post"),
            "posted_date": month, "url": url or f"https://news.ycombinator.com/item?id={c.get('id')}",
            "post_text": txt[:300],
        })
        if len(out) >= max_results:
            break
    return out


def _match_any(text: str, toks: list[str]) -> bool:
    if not toks:
        return True
    low = (text or "").lower()
    return any(t in low for t in toks)


# ── Reddit r/forhire + r/hiring (OAuth — .json/.rss now 403 without a token) ──────
# Reddit hard-blocks unauthenticated programmatic access at the edge (403 on every
# .json/.rss variant, direct or via residential proxy). The only reliable path is
# OAuth: a registered app token against oauth.reddit.com. Set in config:
#   MESA_REDDIT_CLIENT_ID / MESA_REDDIT_SECRET  (from reddit.com/prefs/apps)
#   MESA_REDDIT_USER / MESA_REDDIT_PASS         (optional — enables password grant)
_SUBS = ("forhire", "hiring", "jobbit", "remotejs")
_TOKEN: dict = {"val": None, "exp": 0.0}
_COMP_RE = re.compile(r"(?:at|@|company[:\-]?)\s+([A-Z][\w&.\- ]{2,40})")
_COMP_HIRING_RE = re.compile(r"^([A-Z][\w&.\- ]{2,40}?)\s+is\s+hiring", re.I)
_SALARY_RE = re.compile(r"(\$\s?\d[\d,]*(?:\s?[-–]\s?\$?\d[\d,]*)?\s?(?:k|/hr|/hour|per hour|/yr|k?\s?usd)?|"
                        r"₹\s?\d[\d,]*|€\s?\d[\d,]*)", re.I)
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")


def _reddit_headers():
    return {"User-Agent": (getattr(settings, "MESA_REDDIT_UA", "") or "mesa-hiring-signal/0.1 (by Studojo)")}


def _reddit_token() -> str | None:
    import time
    cid = (getattr(settings, "MESA_REDDIT_CLIENT_ID", "") or "").strip()
    sec = (getattr(settings, "MESA_REDDIT_SECRET", "") or "").strip()
    if not cid or not sec:
        return None
    if _TOKEN["val"] and _TOKEN["exp"] > time.time() + 30:
        return _TOKEN["val"]
    user = (getattr(settings, "MESA_REDDIT_USER", "") or "").strip()
    pw = (getattr(settings, "MESA_REDDIT_PASS", "") or "").strip()
    data = ({"grant_type": "password", "username": user, "password": pw}
            if user and pw else {"grant_type": "client_credentials"})
    try:
        r = httpx.post("https://www.reddit.com/api/v1/access_token", data=data,
                       auth=(cid, sec), headers=_reddit_headers(), timeout=_T, verify=False)
        if r.status_code != 200:
            logger.warning("[MESA] reddit token HTTP %s: %s", r.status_code, r.text[:120])
            return None
        j = r.json()
        _TOKEN["val"] = j.get("access_token")
        _TOKEN["exp"] = time.time() + int(j.get("expires_in", 3600))
        return _TOKEN["val"]
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] reddit token: %s", e)
        return None


def _parse_reddit_post(d: dict, sub: str) -> dict | None:
    title = d.get("title") or ""
    if "[hiring]" not in title.lower() and sub == "forhire":
        return None
    if "[for hire]" in title.lower() or "[task]" in title.lower():
        return None
    body = f"{title}\n{d.get('selftext') or ''}"
    role = re.sub(r"^\s*\[hiring\]\s*", "", title, flags=re.I).strip()
    cm = _COMP_HIRING_RE.search(role) or _COMP_RE.search(body)
    company = (cm.group(1).strip() if cm else "") or f"u/{d.get('author', '')}"
    sal = _SALARY_RE.search(body)
    em = _EMAIL_RE.search(d.get("selftext") or "")
    try:
        dt = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
    except Exception:
        dt = None
    return {
        "external_id": f"rd_{d.get('id')}", "title": role[:120] or title,
        "company": company[:60], "location": "Remote" if "remote" in body.lower() else "See post",
        "posted_date": dt.date().isoformat() if dt else "",
        "url": "https://www.reddit.com" + (d.get("permalink") or ""),
        "apply_link": (f"mailto:{em.group(0)}" if em else ""),
        "salary": (sal.group(1) if sal else ""),
        "author": f"u/{d.get('author', '')}",
        "post_text": (d.get("selftext") or "")[:300],
    }


def scrape_reddit(keywords, location, date_posted="week", workplace_types=None,
                  experience_levels=None, max_results=60):
    token = _reddit_token()
    if not token:
        logger.info("[MESA] reddit: no OAuth creds (MESA_REDDIT_CLIENT_ID/SECRET) — skipping")
        return []
    hdr = {**_reddit_headers(), "Authorization": f"bearer {token}"}
    q = (keywords or "").strip()
    twindow = {"24h": "day", "week": "week", "month": "month", "any": "all"}.get(date_posted, "week")
    out: list[dict] = []
    for sub in _SUBS:
        if len(out) >= max_results:
            break
        url = (f"https://oauth.reddit.com/r/{sub}/search?q={quote(q)}&restrict_sr=1"
               f"&sort=new&t={twindow}&limit=25")
        try:
            r = httpx.get(url, headers=hdr, timeout=_T, verify=False, follow_redirects=True)
            if r.status_code != 200:
                logger.info("[MESA] reddit r/%s HTTP %s", sub, r.status_code)
                continue
            children = r.json().get("data", {}).get("children", [])
        except Exception as e:  # noqa: BLE001
            logger.warning("[MESA] reddit r/%s: %s", sub, e)
            continue
        for ch in children:
            row = _parse_reddit_post(ch.get("data", {}), sub)
            if row:
                out.append(row)
                if len(out) >= max_results:
                    break
    return out
