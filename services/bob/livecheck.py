"""Free liveness + live-job-search via LinkedIn guest endpoints — zero credits.

Root cause this kills: web-search indexes lag reality, so postings arrive
looking live and ship dead (GM / Eidovis / Webs IT, run 10). LinkedIn's
logged-out guest endpoints tell the truth for free:

  jobs-guest/jobs/api/jobPosting/{id}
      'closed-job' marker            -> closed (banner rendered)
      'apply-button' present         -> open
      NEITHER                        -> closed. Promoted/offsite postings hide
      the closed banner from guests but also drop the apply button; a live
      posting always renders one (validated against run-10 dead jobs + a live
      control on 2026-07-08).

  jobs-guest/jobs/api/seeMoreJobPostings/search
      live job cards by keyword/location/recency — fresher than any web index,
      which makes it the preferred SOURCE for LinkedIn jobs, not just a check.
"""

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_LI_JOB_ID = re.compile(r"linkedin\.com/jobs/view/(?:[^\s/?#]*?-)?(\d{9,})", re.IGNORECASE)
_BOARD_HOST = re.compile(r"jobs\.ashbyhq\.com|greenhouse\.io|jobs\.lever\.co", re.IGNORECASE)


def _fetch_guest_job(job_id: str) -> tuple[str, str]:
    """Fetch a job's guest HTML. Returns (body, err); err 'removed' on 404.
    The guest endpoints soft-rate-limit by alternating real payloads with a
    ~26-byte empty stub, so a tiny 200 means retry, not 'no data'."""
    for attempt in range(3):
        try:
            r = requests.get(
                f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
                headers=_HEADERS, timeout=_TIMEOUT,
            )
        except requests.RequestException as e:
            return "", f"fetch failed ({e.__class__.__name__})"
        if r.status_code in (404, 410):
            return "", "removed"
        if r.status_code == 200 and len(r.text) >= 500:
            return r.text, ""
        time.sleep(0.8 * (attempt + 1))
    return "", "guest endpoint kept returning empty stubs"


def _strip_tags(fragment: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def read_job(url: str) -> dict:
    """Full free read of a LinkedIn job's guest page: liveness, role facts,
    DESCRIPTION (for function confirmation) and the JOB POSTER when the hirer
    enabled 'message the job poster' (rendered logged-out too — validated on
    Meetxo/Prashob P, 2026-07-09). Raises LinkedInSearchError on fetch failure."""
    m = _LI_JOB_ID.search(url or "")
    if not m:
        raise LinkedInSearchError("not a linkedin job url")
    body, err = _fetch_guest_job(m.group(1))
    if err == "removed":
        return {"status": "closed", "reason": "posting removed (404)"}
    if not body:
        raise LinkedInSearchError(err)

    if re.search(r"no longer accepting applications|closed-job", body, re.IGNORECASE):
        status, reason = "closed", "no longer accepting applications"
    elif "apply-button" in body:
        status, reason = "open", "apply button live"
    elif "message-the-recruiter" in body:
        # Easy-Apply/DM jobs hide the apply button from guests but render the
        # poster module — the module only exists on live postings.
        status, reason = "open", "job-poster module live (Easy Apply job)"
    else:
        status, reason = "closed", "no apply affordance on guest page (expired promoted posting)"

    def _pick(rx):
        mm = re.search(rx, body, re.S)
        return _strip_tags(mm.group(1))[:200] if mm else ""

    out = {
        "status": status, "reason": reason,
        "title": _pick(r'top-card-layout__title[^>]*>\s*(.*?)</'),
        "company": _pick(r'topcard__org-name-link[^>]*>\s*(.*?)</a>'),
        "location": _pick(r'topcard__flavor--bullet[^>]*>\s*(.*?)</'),
        "posted": _pick(r'posted-time-ago__text[^>]*>\s*(.*?)</'),
    }
    md = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', body, re.S)
    out["description"] = _strip_tags(md.group(1))[:1500] if md else ""
    if out["status"] == "open" and re.search(r"month|[2-9]\s*weeks", out["posted"], re.IGNORECASE):
        out["status"], out["reason"] = "closed", f"stale posting ({out['posted']}); product standard is max ~1 week"

    # Job-poster module ("message the job poster") — present when the hirer
    # opted in; the poster is a T1 contact.
    i = body.lower().find("message the job poster")
    if i == -1:
        i = body.find("message-the-recruiter")
    if i != -1:
        seg = body[max(0, i - 200): i + 3000]
        name_m = re.search(r"view ([^<>\"]{2,60}?)[’']s profile", seg)
        slug_m = re.search(r"(?:%2Fin%2F|/in/)([A-Za-z0-9\-_%.]+)", seg)
        headline = ""
        for t in re.findall(r">\s*([^<>]{5,160}?)\s*<", seg):
            t = re.sub(r"\s+", " ", t).strip()
            if name_m and t == name_m.group(1).strip():
                continue
            low = t.lower()
            if any(w in low for w in ("profile", "message", "sign in", "join now", "third-party")):
                continue
            # First substantive non-name text node after the name is the headline
            # (formats vary: "TA Specialist @Zycus", "Director- X at Y", "A | B").
            headline = t
            break
        if name_m:
            out["poster"] = {
                "name": name_m.group(1).strip()[:80],
                "headline": headline[:150],
                "profile_url": f"https://www.linkedin.com/in/{slug_m.group(1)}" if slug_m else "",
            }
    return out


def linkedin_job_liveness(url: str) -> tuple[str, str]:
    """Return (status, reason) for a linkedin.com/jobs/view URL.

    status: 'open' | 'closed' | 'unknown' (unknown = could not verify, let pass)
    """
    m = _LI_JOB_ID.search(url or "")
    if not m:
        return "unknown", "not a linkedin job url"
    body, err = _fetch_guest_job(m.group(1))
    if err == "removed":
        return "closed", "posting removed (404)"
    if not body:
        return "unknown", err or "guest endpoint kept returning empty stubs"
    if re.search(r"no longer accepting applications|closed-job", body, re.IGNORECASE):
        return "closed", "no longer accepting applications"
    # Age gate: a technically-open posting from a month+ ago is usually a
    # forgotten listing, not active hiring (client feedback 2026-07-09: sheet
    # shipped 2-month-old "open" jobs). Product standard is max ~1 week.
    m_age = re.search(r"posted-time-ago__text[^>]*>\s*([^<]+)", body)
    age = m_age.group(1).strip() if m_age else ""
    if re.search(r"month|[2-9]\s*weeks", age, re.IGNORECASE):
        return "closed", f"stale posting ({age}); product standard is max ~1 week"
    if "apply-button" in body:
        return "open", f"apply button live ({age or 'age n/a'})"
    if "message-the-recruiter" in body:
        return "open", f"job-poster module live, Easy Apply ({age or 'age n/a'})"
    return "closed", "no apply affordance on guest page (expired promoted posting)"


def evidence_gate(url: str) -> tuple[str, str]:
    """Liveness verdict for an evidence URL. Only checks URL families where a
    free deterministic check exists; everything else passes as 'unknown'."""
    u = (url or "").strip()
    if not u:
        return "unknown", ""
    if _LI_JOB_ID.search(u):
        return linkedin_job_liveness(u)
    if _BOARD_HOST.search(u):
        from services.bob import job_boards
        try:
            roles = job_boards.fetch_board(u)
        except job_boards.BoardError as e:
            return "closed", f"job board check failed: {e}"
        except Exception as e:  # network etc. — do not block the row
            return "unknown", f"board fetch failed ({e.__class__.__name__})"
        return ("open", f"board live with {len(roles)} roles") if roles else \
               ("closed", "board is live but lists zero open roles")
    return "unknown", ""


class LinkedInSearchError(Exception):
    pass


_CARD_URN = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_CARD_TITLE = re.compile(r'base-search-card__title[^>]*>\s*(.*?)\s*</', re.S)
_CARD_COMPANY = re.compile(r'base-search-card__subtitle[^>]*>.*?>\s*(.*?)\s*</a>', re.S)
_CARD_LOCATION = re.compile(r'job-search-card__location[^>]*>\s*(.*?)\s*</', re.S)
_CARD_TIME = re.compile(r'<time[^>]*datetime="([0-9-]+)"')


def search_linkedin_jobs(keywords: str, location: str, hours_back: int = 0,
                         limit: int = 15) -> list[dict]:
    """Search LinkedIn's live guest job index. Free, and results are current
    (the index that renders these cards is the one users see)."""
    params = {"keywords": keywords, "location": location}
    if hours_back:
        params["f_TPR"] = f"r{int(hours_back) * 3600}"
    html_text = ""
    saw_real_page = False
    for attempt in range(4):
        try:
            r = requests.get(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params=params, headers=_HEADERS, timeout=_TIMEOUT + 5,
            )
        except requests.RequestException as e:
            raise LinkedInSearchError(f"fetch failed ({e.__class__.__name__})")
        if r.status_code != 200:
            raise LinkedInSearchError(f"guest search returned {r.status_code}")
        if "data-entity-urn" in r.text:
            html_text = r.text
            break
        if len(r.text) >= 500:
            saw_real_page = True  # a real page without job cards = genuinely 0 results
        time.sleep(0.8 * (attempt + 1))  # ~26-byte empty stub = soft rate limit, retry
    if not html_text:
        if saw_real_page:
            return []
        # Only stubs seen: this is RATE LIMITING, not an empty result set. The
        # caller must not mistake it for 0 results and broaden its query — that
        # exact confusion turned a frontend-intern mandate into bare "intern".
        raise LinkedInSearchError(
            "guest index is rate-limiting right now (empty stubs). This is NOT a 0-result "
            "answer. Retry the SAME query after other work; do not broaden keywords."
        )
    marks = list(_CARD_URN.finditer(html_text))
    jobs: list[dict] = []
    for i, m in enumerate(marks[:limit]):
        seg = html_text[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(html_text)]
        def _pick(rx):
            mm = rx.search(seg)
            return re.sub(r"<[^>]+>", "", mm.group(1)).strip() if mm else ""
        jobs.append({
            "job_id": m.group(1),
            "url": f"https://www.linkedin.com/jobs/view/{m.group(1)}",
            "title": _pick(_CARD_TITLE),
            "company": _pick(_CARD_COMPANY),
            "location": _pick(_CARD_LOCATION),
            "posted": _pick(_CARD_TIME),
        })
    return jobs
