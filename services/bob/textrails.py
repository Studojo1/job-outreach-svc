"""Deterministic text/URL rails shared by the agent and the staged pipeline.

Pure functions and compiled patterns only — no DB, no config, no network —
so every rule here is unit-testable and importable from anywhere (including
tests that run without app env). Moved verbatim from agent.py; agent.py
re-imports these under its original private names.
"""

import re

# The model transcribes URLs from scraped pages, and scraped pages lie:
# LinkedIn's logged-out HTML anonymizes company links as /company/unavailable,
# wraps links in signup redirects, and postings expire. Prompt rules reduce
# these errors; these rails guarantee invalid URLs never enter a table cell.

GARBAGE_URL = re.compile(
    r"linkedin\.com/(company|school)/unavailable"   # logged-out anonymized placeholder
    r"|linkedin\.com/signup"                          # signup/cold-join redirect wrappers
    r"|/cold-join"
    r"|linkedin\.com/authwall"
    r"|lnkd\.in/"                                     # shortener — target unknown
    r"|(?:/|\.)t\.me/|telegram\.me/"                  # messenger apply links = scam-grade
    r"|wa\.me/|chat\.whatsapp\.com/|api\.whatsapp\.com/"
    r"|indeed\.[a-z.]+/(?:q-|jobs\?|m/jobs)"          # indeed SEARCH pages, not postings
    r"|jobs\.ashbyhq\.com/?(?:[?#][^\s]*)?$"          # bare board roots carry no company
    r"|boards\.greenhouse\.io/?(?:[?#][^\s]*)?$"
    r"|jobs\.lever\.co/?(?:[?#][^\s]*)?$",
    re.IGNORECASE,
)
# Domains that are never a company's own website (job boards, socials, messengers).
NON_COMPANY_SITE = re.compile(
    r"linkedin\.com|(?:^|//|\.)x\.com|twitter\.com|indeed\.|naukri\.com|ashbyhq\.com"
    r"|greenhouse\.io|lever\.co|wellfound\.com|glassdoor|instagram\.com|facebook\.com"
    r"|youtube\.com|(?:/|\.)t\.me/",
    re.IGNORECASE,
)
# Placeholder "company" names that must never become rows.
PLACEHOLDER_COMPANY = re.compile(r"\b(startup|stealth|unknown|unnamed|various|n/?a)\b", re.IGNORECASE)
# Known internship-spam orgs: NGO "fundraising internships" and pay-to-intern
# mills that flood Internshala/Unstop under every category label (a run once
# shipped these as "Machine Learning Intern"). Never rows, whatever the title.
SPAM_ORGS = re.compile(
    r"nayepankh|basti\s*ki\s*pathshala|maxgen\s*techno|hamari\s*pahchan|muskurahat"
    r"|kshitiksha|prabodhini\s*foundation|unschool|corizo|acmegrade|verzeo",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://[^\s;,\"'<>()\[\]]+")
# Keys that must hold at most ONE link.
SINGLE_URL_KEYS = re.compile(r"(_url$|^website$)", re.IGNORECASE)

CONTACT_KEYS = ("contact_name", "contact_title", "tier", "contact_linkedin_url",
                "contact_email", "linkedin_url")


def post_author(url: str) -> str:
    """Author handle/slug from a social evidence URL ('' when not a post)."""
    m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/", url, re.IGNORECASE)
    if m:
        return "x:" + m.group(1).lower()
    m = re.search(r"linkedin\.com/posts/([^_/?#]+)", url, re.IGNORECASE)
    if m:
        return "li:" + m.group(1).lower()
    return ""


def coerce_score(v) -> int | None:
    try:
        return int(round(float(str(v).strip().rstrip("%"))))
    except (ValueError, TypeError):
        return None


def norm_company(v) -> str:
    """Normalized company key: parentheticals dropped so
    'Composio (Ashby job board)' == 'Composio'."""
    s = re.sub(r"\([^)]*\)", " ", str(v or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def norm_person(v) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v or "").lower())


def norm_url(u: str) -> str:
    """Canonical form for cross-source URL dedupe."""
    u = (u or "").split("?")[0].split("#")[0].rstrip("/").lower()
    return u.replace("https://www.", "https://").replace("https://in.", "https://")


def contact_collision(clean: dict, company_norm: str, owners: dict) -> str:
    """A real hiring contact belongs to ONE company. If this row's contact_name
    is already assigned to a DIFFERENT company in the table, it is a
    misattribution (the Pranit-Mehta-on-two-companies bug). Returns the owning
    company name if it collides, else ''. Mutates nothing."""
    cn = norm_person(clean.get("contact_name"))
    if not cn:
        return ""
    owner = owners.get(cn)
    if owner and owner[0] != company_norm:
        return owner[1]
    return ""


def valid_url(u: str) -> bool:
    if GARBAGE_URL.search(u):
        return False
    # LinkedIn job URLs carry a long numeric id; a short one means truncation.
    m = re.search(r"linkedin\.com/jobs/view/[^\s]*?(\d+)/?$", u)
    if m and len(m.group(1)) < 9:
        return False
    return True


def strip_em_dashes(t: str) -> str:
    """Product rule: no em/en dashes anywhere in Bob's output, ever."""
    t = re.sub(r"(?<=\d)\s*[—–]\s*(?=\d)", "-", t)   # numeric ranges: 2–5 → 2-5
    return re.sub(r"\s*[—–]+\s*", ", ", t)


def sanitize_cells(cells: dict) -> tuple[dict, list[str]]:
    """Strip invalid URLs and em dashes from cell values. Returns (clean_cells, removals)."""
    removed: list[str] = []
    out: dict = {}
    for k, v in cells.items():
        if not isinstance(v, str):
            out[k] = v
            continue
        v = strip_em_dashes(v)
        if "http" not in v:
            out[k] = v
            continue
        urls = URL_RE.findall(v)
        keep = [u.rstrip(".,;") for u in urls if valid_url(u.rstrip(".,;"))]
        bad = [u for u in urls if not valid_url(u.rstrip(".,;"))]
        if "evidence" in k.lower():
            # A LinkedIn company/school page is never evidence of anything, and
            # an X profile URL (no /status/) is a useless stub, not a post.
            pages = [u for u in keep if re.search(r"linkedin\.com/(company|school)/", u, re.IGNORECASE)
                     or (re.search(r"(?:^|\.)((x|twitter)\.com)/", u, re.IGNORECASE)
                         and "/status/" not in u.lower())]
            keep = [u for u in keep if u not in pages]
            bad += pages
        for b in bad:
            removed.append(f"{k}: {b[:120]}")
        if re.fullmatch(r"(company_)?website", k, re.IGNORECASE):
            # website = the company's OWN domain; boards/socials never qualify.
            nonsite = [u for u in keep if NON_COMPANY_SITE.search(u)]
            keep = [u for u in keep if u not in nonsite]
            for b in nonsite:
                removed.append(f"{k}: {b[:80]} is a job board or social page, not the company website")
        if "linkedin" in k.lower():
            # A linkedin_* column holds LinkedIn URLs of the right kind, or nothing.
            want = r"linkedin\.com/in/" if "contact" in k.lower() else r"linkedin\.com/(company|school|showcase)/"
            wrong = [u for u in keep if not re.search(want, u, re.IGNORECASE)]
            keep = [u for u in keep if u not in wrong]
            for b in wrong:
                removed.append(f"{k}: {b[:90]} does not belong in a LinkedIn column (X links and non-LinkedIn URLs are never valid here)")
        if SINGLE_URL_KEYS.search(k):
            # URL-typed field: exactly the first valid URL, or empty.
            if len(keep) > 1:
                removed.append(f"{k}: kept first URL, dropped {len(keep) - 1} extra")
            out[k] = keep[0] if keep else ""
        else:
            nv = v
            for b in bad:
                nv = nv.replace(b, "").strip(" ;,")
            out[k] = nv
    return out, removed


_AGE_UNITS = {"minute": 1 / 1440, "min": 1 / 1440, "hour": 1 / 24, "hr": 1 / 24, "h": 1 / 24,
              "day": 1.0, "d": 1.0, "week": 7.0, "wk": 7.0, "w": 7.0, "month": 30.0, "mo": 30.0}


def parse_age_days(posted: str, now=None) -> float | None:
    """Posting-age in days from the free-text forms sources actually emit:
    '14h', '5d', '3 days ago', '2 weeks ago', '1 month ago', 'just now',
    'yesterday', '2026-07-03', 'Jul 02'. None when unparseable — callers must
    treat None as unknown, never as fresh."""
    from datetime import datetime, timezone
    t = (posted or "").strip().lower().replace("posted", "").strip()
    if not t:
        return None
    if re.search(r"just now|today|hours? ago|minutes? ago|^now$", t):
        m = re.search(r"(\d+)\s*(hour|minute)", t)
        if m:
            return int(m.group(1)) * _AGE_UNITS[m.group(2)]
        return 0.0
    if "yesterday" in t:
        return 1.0
    m = re.fullmatch(r"(\d+)\s*(minute|min|hour|hr|h|day|d|week|wk|w|month|mo)s?\.?(\s*ago)?", t)
    if m:
        return int(m.group(1)) * _AGE_UNITS[m.group(2)]
    m = re.search(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s*ago", t)
    if m:
        return int(m.group(1)) * _AGE_UNITS[m.group(2)]
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        then = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        return max(0.0, (now - then).total_seconds() / 86400)
    m = re.fullmatch(r"([a-z]{3,9})\s+(\d{1,2})", t)
    if m:
        months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        try:
            mo = months.index(m.group(1)[:3]) + 1
        except ValueError:
            return None
        then = datetime(now.year, mo, int(m.group(2)), tzinfo=timezone.utc)
        if then > now:  # "Dec 28" seen in January = last year
            then = then.replace(year=now.year - 1)
        return max(0.0, (now - then).total_seconds() / 86400)
    return None
