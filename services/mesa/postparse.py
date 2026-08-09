"""Parsing rails for LinkedIn hiring-post text — no auth, no network (except resolve_short).

Three capabilities proven in the Polaris/Futurense placement runs:

1. fold_unicode — hiring posts style field labels with Mathematical Alphanumeric
   glyphs (𝗖𝗼𝗺𝗽𝗮𝗻𝘆:), which regexes can't match. Folding them to ASCII took one
   real run from 19 parsed jobs to 44 from the same raw posts.

2. split_digest — many high-yield posts are multi-job digests
   ("1) Company - X  Role - Y  Stipend - Z  2) Company - ..."); parsing them as
   one post loses every job but the first. Splits on Company markers and
   extracts labeled fields per block.

3. resolve_short — lnkd.in / bit.ly apply links usually 302 straight to the
   real job page over plain HTTP; the resolved URL is the sendable artifact.
   (JS-interstitial shorteners resolve to themselves — callers keep the original.)
"""

import logging
import re

import httpx

logger = logging.getLogger(__name__)

# ── 1. unicode folding ────────────────────────────────────────────────────────
# Mathematical Alphanumeric Symbols block: letters come in (upper, lower) runs
# of 26; digits live at the end of the block.
_ALPHA_RUNS = (
    (0x1D400, 0x1D419, "A"), (0x1D41A, 0x1D433, "a"),   # bold
    (0x1D434, 0x1D44D, "A"), (0x1D44E, 0x1D467, "a"),   # italic
    (0x1D468, 0x1D481, "A"), (0x1D482, 0x1D49B, "a"),   # bold italic
    (0x1D5A0, 0x1D5B9, "A"), (0x1D5BA, 0x1D5D3, "a"),   # sans
    (0x1D5D4, 0x1D5ED, "A"), (0x1D5EE, 0x1D607, "a"),   # sans bold
    (0x1D608, 0x1D621, "A"), (0x1D622, 0x1D63B, "a"),   # sans italic
    (0x1D670, 0x1D689, "A"), (0x1D68A, 0x1D6A3, "a"),   # monospace
)


def fold_unicode(s: str) -> str:
    """Map Mathematical Alphanumeric glyphs to plain ASCII; pass the rest through."""
    if not s or max(s) < "\U0001D400":
        return s or ""
    out = []
    for ch in s:
        o = ord(ch)
        if 0x1D400 <= o <= 0x1D7FF:
            for lo, hi, base in _ALPHA_RUNS:
                if lo <= o <= hi:
                    out.append(chr(ord(base) + o - lo))
                    break
            else:
                out.append(chr(ord("0") + (o - 0x1D7CE) % 10) if 0x1D7CE <= o <= 0x1D7FF else " ")
        else:
            out.append(ch)
    return "".join(out)


# ── 2. multi-job digest splitting ─────────────────────────────────────────────
_FIELD_STOP = ["Company", "Role", "Position", "Profile", "Batch", "Stipend", "CTC", "Salary",
               "Pay", "Package", "Location", "Eligib", "Experience", "Apply", "Qualif",
               "Skills", "Type", "Duration", "Mode", "Job", "Deadline", "Notice"]
_STOP_ALT = "|".join(_FIELD_STOP)


def _field(block: str, names: list[str]) -> str:
    pat = r"(?:%s)\s*[-:]\s*(.+?)(?=\s+(?:%s)\s*[-:]|$)" % ("|".join(names), _STOP_ALT)
    m = re.search(pat, block, re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def split_digest(body: str) -> list[dict]:
    """Split a multi-job digest post into per-job dicts.

    Returns [] when the post is not a digest (fewer than 2 'Company -/:' blocks),
    so callers fall back to single-post parsing. Each dict carries the labeled
    fields found in its block: company, role, stipend, batch, location, apply_url.
    """
    text = fold_unicode(body)
    parts = re.split(r"(?=(?:\d+[\).]\s*)?Company\s*[-:])", text)
    blocks = [p for p in parts if re.search(r"Company\s*[-:]", p)]
    if len(blocks) < 2:
        return []
    jobs = []
    for b in blocks:
        company = _field(b, ["Company"])
        if not company or len(company) > 45:
            continue
        role = _field(b, ["Role", "Position", "Profile"])
        m = re.search(r"https?://\S+", b)
        jobs.append({
            "company": company,
            "role": role,
            "stipend": _field(b, ["Stipend", "CTC", "Salary", "Pay", "Package"]),
            "batch": _field(b, ["Batch", "Eligib"]),
            "location": _field(b, ["Location"]),
            "apply_url": m.group(0).rstrip(").,") if m else "",
        })
    return jobs


# ── 3. shortener resolution ───────────────────────────────────────────────────
_SHORT_HOSTS = ("lnkd.in", "bit.ly", "cutt.ly", "rb.gy", "tinyurl.", "t.ly", "shorturl.")
_DEAD_ENDS = ("linkedin.com/authwall", "linkedin.com/login")
_cache: dict[str, str] = {}


def resolve_short(url: str, timeout: float = 10.0) -> str:
    """Follow a shortened apply link to its destination. Returns the original url
    when resolution fails, dead-ends on a login wall, or lands back on a shortener
    (JS interstitial) — never raises, in-process cached."""
    if not url or not any(h in url for h in _SHORT_HOSTS):
        return url
    if url in _cache:
        return _cache[url]
    final = url
    try:
        r = httpx.get(url, follow_redirects=True, timeout=timeout, verify=False,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                             "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"})
        dest = str(r.url)
        bare = dest.rstrip("/").lower()
        if (dest.startswith("http")
                and not any(h in dest for h in _SHORT_HOSTS)
                and not any(d in dest for d in _DEAD_ENDS)
                and bare not in ("https://www.linkedin.com", "https://linkedin.com")):
            final = dest.split("?")[0]
    except Exception as e:  # noqa: BLE001
        logger.debug("[MESA_POSTPARSE] resolve_short failed for %s: %s", url, e)
    _cache[url] = final
    return final


# ── 4. posted-date normalization ──────────────────────────────────────────────
# Every source states freshness differently: ISO dates (LinkedIn jobs), relative
# post ages ("3d", "2 weeks ago", "30+ days ago"), or nothing. Feed boards also
# ignore date filters entirely, so the runner gates on this at ingest.
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_REL_RE = re.compile(r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|w\b|month|mo\b|d\b|h\b|m\b)", re.I)


def posted_age_days(posted, now=None) -> float | None:
    """Days since posting from any source's `posted_date` string; None if unknowable."""
    from datetime import datetime, timezone
    if not posted:
        return None
    s = str(posted).strip().lower()
    now = now or datetime.now(timezone.utc)
    m = _ISO_RE.match(s)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return max(0.0, (now - dt).total_seconds() / 86400)
        except ValueError:
            return None
    if any(k in s for k in ("just now", "today", "few hours")):
        return 0.0
    if "yesterday" in s:
        return 1.0
    m = _REL_RE.search(s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)[0]  # minute/min/m, hour/hr/h, day/d, week/w, month/mo
    if m.group(2).startswith("mo"):
        return float(n * 30)
    return {"m": 0.0, "h": n / 24.0, "d": float(n), "w": float(n * 7)}.get(unit, None)
