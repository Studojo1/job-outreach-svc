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


# ── Reddit r/forhire + r/hiring ───────────────────────────────────────────────
def _reddit_proxy():
    url = (getattr(settings, "LINKEDIN_PROXY_URL", "") or "").strip()
    return url or None


def scrape_reddit(keywords, location, date_posted="week", workplace_types=None,
                  experience_levels=None, max_results=60):
    q = (keywords or "").strip()
    proxy = _reddit_proxy()
    out: list[dict] = []
    for sub in ("forhire", "hiring"):
        if len(out) >= max_results:
            break
        url = (f"https://www.reddit.com/r/{sub}/search.json?q={httpx.QueryParams({'x': q + ' hiring'})['x']}"
               f"&restrict_sr=1&sort=new&t=week&limit=25")
        try:
            r = httpx.get(url, headers=_H, timeout=_T, verify=False, follow_redirects=True, proxy=proxy)
            if r.status_code != 200:
                logger.info("[MESA] reddit r/%s HTTP %s", sub, r.status_code)
                continue
            children = r.json().get("data", {}).get("children", [])
        except Exception as e:  # noqa: BLE001
            logger.warning("[MESA] reddit r/%s: %s", sub, e)
            continue
        for ch in children:
            d = ch.get("data", {})
            title = d.get("title") or ""
            # forhire tags posts [Hiring]; skip [For Hire]/[Task] seekers
            if sub == "forhire" and "[hiring]" not in title.lower():
                continue
            try:
                dt = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
            except Exception:
                dt = None
            clean_title = re.sub(r"^\[hiring\]\s*", "", title, flags=re.I).strip()
            out.append({
                "external_id": f"rd_{d.get('id')}", "title": clean_title[:120] or title,
                "company": (d.get("author") or "-"), "location": "Remote" if "remote" in title.lower() else "See post",
                "posted_date": dt.date().isoformat() if dt else "",
                "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                "post_text": (d.get("selftext") or "")[:300],
            })
            if len(out) >= max_results:
                break
    return out
