"""X/Twitter hiring signals for Mesa — best-effort.

X has no free search API and actively blocks scraping, so this rides public
Nitter mirrors' RSS search (`/search/rss?q=...`) through the residential proxy,
trying each mirror until one answers. If every mirror is down it returns [] (no
error) — treat this like naukri/indeed: a bonus when it works, never load-bearing.

Same source contract as sources.py:
    scrape(keywords, location, date_posted, workplace_types, experience_levels, max_results)
    -> [{external_id, title, company, location, posted_date, url, author, post_text}]
"""
import html as ihtml
import logging
import re
from urllib.parse import quote

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")}
_T = 15.0
# Public Nitter mirrors (volatile — order = preference; dead ones are skipped).
_MIRRORS = ["nitter.net", "nitter.poast.org", "nitter.privacydev.net", "xcancel.com",
            "nitter.privacyredirect.com", "nitter.tiekoetter.com"]
_HIRING_RE = re.compile(r"\b(hiring|we'?re hiring|now hiring|join (our|the) team|"
                        r"open (role|position)|apply|dm me|drop your|send your (cv|resume))\b", re.I)


def _proxy():
    return (getattr(settings, "LINKEDIN_PROXY_URL", "") or "").strip() or None


def _items(xml: str) -> list[dict]:
    out = []
    for block in re.findall(r"<item>(.*?)</item>", xml or "", re.S):
        def _tag(t):
            m = re.search(rf"<{t}>(.*?)</{t}>", block, re.S)
            v = m.group(1) if m else ""
            v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", v, flags=re.S)
            return ihtml.unescape(re.sub(r"<[^>]+>", " ", v)).strip()
        out.append({"title": _tag("title"), "link": _tag("link"),
                    "creator": _tag("dc:creator") or _tag("creator"), "pubDate": _tag("pubDate"),
                    "desc": _tag("description")})
    return out


def scrape_twitter(keywords, location, date_posted="week", workplace_types=None,
                   experience_levels=None, max_results=40):
    q = quote(f"{keywords} hiring")
    proxy = _proxy()
    out: list[dict] = []
    for host in _MIRRORS:
        if out:
            break
        url = f"https://{host}/search/rss?f=tweets&q={q}"
        try:
            r = httpx.get(url, headers=_H, timeout=_T, verify=False, follow_redirects=True, proxy=proxy)
            if r.status_code != 200 or "<item>" not in r.text:
                continue
        except Exception as e:  # noqa: BLE001
            logger.debug("[MESA] nitter %s: %s", host, e)
            continue
        for it in _items(r.text):
            text = f"{it['title']} {it['desc']}"
            if not _HIRING_RE.search(text):
                continue
            link = it["link"].replace(host, "twitter.com") if host in it["link"] else it["link"]
            tid = re.search(r"/status/(\d+)", it["link"])
            out.append({
                "external_id": f"tw_{tid.group(1) if tid else abs(hash(it['link']))}",
                "title": it["title"][:120], "company": "-", "author": it["creator"] or "-",
                "location": "See post", "posted_date": (it["pubDate"] or "")[:16],
                "url": link, "post_text": it["desc"][:300],
            })
            if len(out) >= max_results:
                break
    if not out:
        logger.info("[MESA] twitter: all nitter mirrors unavailable")
    return out
