"""Free structured job-board reads for Bob — zero Context.dev credits.

Ashby, Greenhouse and Lever all expose public JSON for their hosted boards.
When evidence links to one of these, reading the real board beats trusting a
post's claim ("16 open roles!") — this is what catches the WisdomAI case where
the only sales role was in San Francisco.
"""

import logging
import re

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 25
_MAX_ROLES = 60


class BoardError(Exception):
    pass


def fetch_board(url: str) -> list[dict]:
    """Return [{title, location}] for a hosted board URL. Raises BoardError."""
    u = (url or "").strip()

    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)", u, re.I)
    if m:
        return _ashby(m.group(1))
    m = re.search(r"(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io/embed/job_board\?for=|greenhouse\.io)/([^/?#]+)", u, re.I)
    if m and "greenhouse" in u.lower():
        return _greenhouse(m.group(1))
    m = re.search(r"jobs\.lever\.co/([^/?#]+)", u, re.I)
    if m:
        return _lever(m.group(1))

    raise BoardError(
        "Not a supported hosted board (ashbyhq/greenhouse/lever). Use scrape_page for other career pages."
    )


def _get_json(url: str):
    r = requests.get(url, timeout=_TIMEOUT, headers={"Accept": "application/json"})
    if not r.ok:
        raise BoardError(f"Board API returned HTTP {r.status_code}")
    return r.json()


def _ashby(org: str) -> list[dict]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    return [
        {"title": j.get("title") or "", "location": j.get("location") or "",
         "department": j.get("department") or ""}
        for j in (data.get("jobs") or [])[:_MAX_ROLES]
    ]


def _greenhouse(org: str) -> list[dict]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs?content=false")
    return [
        {"title": j.get("title") or "",
         "location": ((j.get("location") or {}).get("name") or "")}
        for j in (data.get("jobs") or [])[:_MAX_ROLES]
    ]


def _lever(org: str) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{org}?mode=json")
    if not isinstance(data, list):
        raise BoardError("Unexpected Lever response")
    return [
        {"title": j.get("text") or "",
         "location": ((j.get("categories") or {}).get("location") or "")}
        for j in data[:_MAX_ROLES]
    ]
