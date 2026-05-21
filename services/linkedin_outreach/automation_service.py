"""LinkedIn outreach automation — background daemon for connection requests + follow-ups."""

import asyncio
import logging
import random
import re
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _is_ist_sending_window() -> bool:
    """Return True if current UTC time falls within 9am–6pm IST (UTC+5:30)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    hour = datetime.now(ist).hour
    return 9 <= hour < 18


def _proxy(session_id: str | None = None) -> dict:
    """Build httpx proxy kwarg for Evomi residential proxy.

    Evomi sticky-session URL variants fail with 407 on HTTPS CONNECT tunneling
    (LinkedIn). Plain credential URL works correctly and gives a fresh
    residential IP per request. Rate-limiting (90-240s between sends) is the
    primary anti-ban mechanism, not IP consistency. session_id is kept for
    API compatibility but is not used.
    """
    from core.config import settings
    base_url = (settings.LINKEDIN_PROXY_URL or "").strip()
    if not base_url:
        return {}
    return {"proxy": base_url}

# Conservative daily limit per account (LinkedIn's soft limit is ~100/week)
MAX_DAILY_REQUESTS = 25
# Gap between each send (seconds)
MIN_GAP_SECONDS = 90
MAX_GAP_SECONDS = 240

# Per-campaign poll timestamps — reset on restart, avoids a migration for separate columns
_last_accept_check: dict[int, float] = {}
_last_reply_check: dict[int, float] = {}
_ACCEPT_INTERVAL = 14400  # 4 hours
_REPLY_INTERVAL = 7200    # 2 hours


# ── Voyager helpers ────────────────────────────────────────────────────────────

def _build_cookie_header(li_at: str, jsessionid: str, cookies_blob: str | None = None) -> str:
    """Build the Cookie header string. If cookies_blob is provided (full jar from
    the extension), use it verbatim — LinkedIn's anti-fraud check passes only
    with a complete cookie set. Otherwise fall back to li_at+JSESSIONID only."""
    if cookies_blob:
        try:
            import json as _json
            cookies = _json.loads(cookies_blob)
            # Preserve order; LinkedIn doesn't care, but readability helps debugging
            parts = []
            for c in cookies:
                name = c.get("name")
                value = c.get("value")
                if not name or value is None:
                    continue
                parts.append(f"{name}={value}")
            if parts:
                return "; ".join(parts)
        except Exception:
            pass
    # Fallback — partial set
    return f"li_at={li_at}; JSESSIONID={jsessionid}"


def _headers(li_at: str, jsessionid: str, cookies_blob: str | None = None) -> dict:
    return {
        "Cookie": _build_cookie_header(li_at, jsessionid, cookies_blob),
        "csrf-token": jsessionid,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.linkedin.com/",
        "Origin": "https://www.linkedin.com",
    }


def _decrypt_cookies_blob(token_row) -> str | None:
    """Decrypt the full cookie jar JSON from a LinkedInToken row, if present."""
    if not getattr(token_row, "cookies_blob_enc", None) or not getattr(token_row, "cookies_blob_nonce", None):
        return None
    try:
        from services.linkedin_outreach.crypto import decrypt as _dec
        return _dec(token_row.cookies_blob_enc, token_row.cookies_blob_nonce)
    except Exception as e:
        logger.warning("Could not decrypt cookies_blob: %s", e)
        return None


async def resolve_profile_urn(
    li_at: str, jsessionid: str, profile_url: str,
    session_id: str | None = None,
    cookies_blob: str | None = None,
) -> Optional[str]:
    """Extract fsd_profile URN from a LinkedIn profile URL via Voyager API (~20 KB, not HTML).

    Raises LinkedInAuthError if the session is expired.
    """
    slug = profile_url.rstrip("/").split("/in/")[-1].split("?")[0].split("/")[0]
    try:
        # Voyager identity endpoint — returns JSON with entityUrn containing fsd_profile URN
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, **_proxy(session_id)) as client:
            res = await client.get(
                f"https://www.linkedin.com/voyager/api/identity/profiles/{slug}",
                headers=_headers(li_at, jsessionid, cookies_blob),
            )
        if res.status_code in (401, 403):
            raise LinkedInAuthError(f"Session expired (status {res.status_code}) resolving {profile_url}")
        if res.status_code == 200:
            data = res.json()
            # entityUrn is "urn:li:fsd_profile:ACoAAC..." — extract the ID
            entity_urn = data.get("data", {}).get("entityUrn", "") or data.get("entityUrn", "")
            if "fsd_profile:" in entity_urn:
                return entity_urn.split("fsd_profile:")[-1]
            # Fallback: scan included entities
            for entity in data.get("included", []):
                urn = entity.get("entityUrn", "")
                if "fsd_profile:" in urn:
                    return urn.split("fsd_profile:")[-1]
        # Final fallback: HTML scrape (2× larger but reliable)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, **_proxy(session_id)) as client:
            res2 = await client.get(
                f"https://www.linkedin.com/in/{slug}/",
                headers={**_headers(li_at, jsessionid, cookies_blob), "Accept": "text/html"},
            )
        if "/login" in str(res2.url):
            raise LinkedInAuthError("Session expired (redirected to login page)")
        matches = re.findall(r"fsd_profile:([A-Za-z0-9_-]{30,})", res2.text)
        for m in matches:
            if m != "urn":
                return m
    except LinkedInAuthError:
        raise
    except Exception as e:
        logger.warning("URN resolve failed for %s: %s", profile_url, e)
    return None


class LinkedInAuthError(Exception):
    """Raised when LinkedIn returns 401/403 indicating an expired or invalid session."""


async def send_connection_request(
    li_at: str,
    jsessionid: str,
    profile_urn: str,
    note: str = "",
    session_id: str | None = None,
    profile_url: str | None = None,
    cookies_blob: str | None = None,
) -> bool:
    """Send a LinkedIn connection request via Voyager. Returns True on success.

    Uses a single persistent httpx client for both the seed GET (to obtain a
    JSESSIONID valid for this proxy IP) and the POST, so both requests travel
    through the same proxy connection. LinkedIn's JSESSIONID is IP-bound for
    CSRF, so the seed and the POST must come from the same IP.

    If profile_urn is empty, skips the Voyager API attempts and goes straight
    to the Playwright fallback using profile_url. This handles extension-login
    sessions where Voyager API auth fails due to IP binding but a browser-driven
    UI click still works.

    Raises LinkedInAuthError when the session is expired (401/403).
    """
    # If we have no URN, skip Voyager attempts — they require profileId.
    if not profile_urn and profile_url:
        return await _send_via_playwright(li_at, jsessionid, profile_url, note, None, cookies_blob)
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    )
    # normInvitations (primary, still supported) uses growth.invitation namespace.
    # relationships/invitations (fallback) uses relationships.invitation namespace.
    norm_payload = {
        "trackingId": _tracking_id(),
        "message": note[:300] if note else "",
        "invitations": [],
        "excludeInvitations": [{}],
        "invitee": {
            "com.linkedin.voyager.growth.invitation.InviteeProfile": {
                "profileId": profile_urn,
            }
        },
    }
    rel_payload = {
        "trackingId": _tracking_id(),
        "invitations": [],
        "excludeInvitations": [],
        "invitee": {
            "com.linkedin.voyager.relationships.invitation.InviteeProfile": {
                "profileId": profile_urn,
            }
        },
        "message": note[:300] if note else "",
    }

    # If a full cookie jar from the extension is available, use it for the seed GET.
    # That way LinkedIn sees a complete authentic session (bcookie, bscookie, lidc,
    # li_mc, lang, liap, etc.) rather than a bare li_at-only "stolen cookies" request.
    seed_cookie_header = _build_cookie_header(li_at, jsessionid, cookies_blob)

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, **_proxy(session_id)) as client:
            # Seed GET — send ONLY li_at (no stale JSESSIONID) so LinkedIn is forced
            # to issue a fresh JSESSIONID for this proxy IP. Sending the stale one
            # causes LinkedIn to accept it as-is and return no new Set-Cookie.
            # A public profile page is used because / doesn't trigger JSESSIONID issuance.
            _fresh_jsessionid = None
            for _canary in ("williamhgates", "jeffweiner08", "reidhoffman"):
                seed = await client.get(
                    f"https://www.linkedin.com/in/{_canary}/",
                    headers={"Cookie": f"li_at={li_at}", "User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
                    follow_redirects=True,
                )
                _fresh_jsessionid = seed.cookies.get("JSESSIONID") or client.cookies.get("JSESSIONID")
                if _fresh_jsessionid:
                    break
            raw_csrf = _fresh_jsessionid or jsessionid
            # LinkedIn stores JSESSIONID as "ajax:XXXX" (with surrounding quotes).
            # The csrf-token header must be the bare value; Cookie header must keep quotes.
            csrf = raw_csrf.strip('"')
            csrf_source = "fresh" if client.cookies.get("JSESSIONID") else "stored"

            # Build Cookie header manually so we know exactly what's sent.
            # Start from the full extension jar (if provided) so every cookie LinkedIn
            # expects is present, then layer in any fresher values from the seed response
            # (JSESSIONID, lidc, bcookie) so the proxy IP gets recognised as a valid session.
            cookie_map: dict[str, str] = {}
            if cookies_blob:
                try:
                    import json as _json
                    for c in _json.loads(cookies_blob):
                        n, v = c.get("name"), c.get("value")
                        if n and v is not None:
                            cookie_map[n] = v
                except Exception:
                    pass
            # Always ensure li_at is present and authoritative from the stored decrypt
            cookie_map["li_at"] = li_at
            # Overlay fresher values from the seed response
            for name in ("JSESSIONID", "bcookie", "bscookie", "lidc", "li_mc", "sdui_ver", "lang", "liap"):
                v = client.cookies.get(name)
                if v:
                    cookie_map[name] = v
            cookie_str = "; ".join(f"{n}={v}" for n, v in cookie_map.items())

            logger.info(
                "send_connection_request profileId=%s csrf_source=%s csrf=%s cookie=%s",
                profile_urn, csrf_source, csrf[:20], cookie_str[:150],
            )

            post_headers = {
                "Cookie": cookie_str,
                "csrf-token": csrf,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "x-restli-protocol-version": "2.0.0",
                "x-li-lang": "en_US",
                "User-Agent": ua,
                "Referer": "https://www.linkedin.com/mynetwork/",
                "Origin": "https://www.linkedin.com",
                "x-li-track": '{"clientVersion":"1.13.14866","mpVersion":"1.13.14866","osName":"web","timezoneOffset":5.5,"timezone":"Asia/Calcutta","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":2,"displayWidth":2560,"displayHeight":1600}',
            }

            # normInvitations (growth.invitation namespace) is the current supported endpoint.
            # relationships/invitations (relationships.invitation namespace) is the fallback.
            attempts = [
                ("https://www.linkedin.com/voyager/api/growth/normInvitations", norm_payload),
                ("https://www.linkedin.com/voyager/api/relationships/invitations", rel_payload),
            ]
            res = None
            for ep, ep_payload in attempts:
                r = await client.post(ep, headers=post_headers, json=ep_payload)
                logger.info(
                    "endpoint=%s status=%d headers=%s body=%s",
                    ep.split("/")[-1], r.status_code,
                    dict(r.headers),
                    r.text[:300],
                )
                if r.status_code in (200, 201):
                    return True
                if r.status_code in (401, 403):
                    raise LinkedInAuthError(f"Session expired (status {r.status_code})")
                res = r

        logger.warning(
            "Voyager API send failed (normInvitations=%s, relationships=%s) — falling back to Playwright",
            "301", "400",
        )
    except LinkedInAuthError:
        raise
    except Exception as voyager_exc:
        logger.warning("Voyager API threw exception — falling back to Playwright: %s", voyager_exc)

    # Playwright fallback: click the Connect button in a real browser
    # Prefer the real profile_url if caller passed it, else reconstruct from URN.
    target_url = profile_url or f"https://www.linkedin.com/in/{profile_urn}/"
    return await _send_via_playwright(li_at, jsessionid, target_url, note, profile_urn, cookies_blob)


async def _send_via_playwright(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    note: str,
    existing_urn: str | None,
    cookies_blob: str | None = None,
) -> bool:
    try:
        from services.linkedin_outreach.playwright_service import playwright_send_invitation
        from core.config import settings
        proxy_url = (settings.LINKEDIN_PROXY_URL or "").strip() or None
        result = await playwright_send_invitation(
            li_at=li_at,
            jsessionid=jsessionid,
            profile_url=profile_url,
            note=note,
            proxy_url=proxy_url,
            existing_urn=existing_urn,
            cookies_blob=cookies_blob,
        )
        if result.get("ok"):
            logger.info("Playwright send succeeded for %s", profile_url)
            return True
        err = result.get("error", "unknown")
        logger.warning("Playwright send failed for %s: %s", profile_url, err)
        return False
    except LinkedInAuthError:
        raise
    except Exception as e:
        logger.error("Playwright send_connection_request failed: %s", e)
        return False


async def send_message(
    li_at: str,
    jsessionid: str,
    profile_urn: str,
    message: str,
    session_id: str | None = None,
) -> bool:
    """Send a LinkedIn DM to a 1st-degree connection."""
    payload = {
        "keyVersion": "LEGACY_INBOX",
        "conversationCreate": {
            "eventCreate": {
                "value": {
                    "com.linkedin.voyager.messaging.create.MessageCreate": {
                        "attributedBody": {
                            "text": message,
                            "attributes": [],
                        },
                        "attachments": [],
                    }
                }
            },
            "recipients": [f"urn:li:fsd_profile:{profile_urn}"],
            "subtype": "MEMBER_TO_MEMBER",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=20, **_proxy(session_id)) as client:
            res = await client.post(
                "https://www.linkedin.com/voyager/api/messaging/conversations",
                headers=_headers(li_at, jsessionid),
                json=payload,
            )
        return res.status_code in (200, 201)
    except Exception as e:
        logger.error("send_message failed: %s", e)
        return False


async def check_connection_accepted(
    li_at: str,
    jsessionid: str,
    profile_urn: str,
    session_id: str | None = None,
) -> bool:
    """Check if a profile is now a 1st-degree connection."""
    try:
        async with httpx.AsyncClient(timeout=15, **_proxy(session_id)) as client:
            res = await client.get(
                f"https://www.linkedin.com/voyager/api/identity/profiles/{profile_urn}/networkinfo",
                headers=_headers(li_at, jsessionid),
            )
        if res.status_code == 200:
            data = res.json()
            distance = data.get("distance", {}).get("value") or data.get("distance", "")
            return str(distance) == "DISTANCE_1"
    except Exception as e:
        logger.warning("check_connection_accepted failed for %s: %s", profile_urn, e)
    return False


async def get_recent_messages(li_at: str, jsessionid: str) -> list[dict]:
    """Fetch recent conversations to detect replies."""
    try:
        async with httpx.AsyncClient(timeout=20, **_proxy()) as client:
            res = await client.get(
                "https://www.linkedin.com/voyager/api/messaging/conversations"
                "?keyVersion=LEGACY_INBOX&q=inbox",
                headers=_headers(li_at, jsessionid),
            )
        if res.status_code == 200:
            data = res.json()
            return data.get("included", [])
    except Exception as e:
        logger.warning("get_recent_messages failed: %s", e)
    return []


def _tracking_id() -> str:
    import base64, os
    return base64.b64encode(os.urandom(16)).decode()


# ── Lead search via Apollo.io (primary) ───────────────────────────────────────

_LOCATION_MAP: dict[str, str] = {
    "India": "India",
    "United States": "United States",
    "United Kingdom": "United Kingdom",
    "UAE / Dubai": "United Arab Emirates",
    "Singapore": "Singapore",
    "Europe": "Europe",
    "Southeast Asia": "Southeast Asia",
    "Global": "",
}

_INDUSTRY_MAP: dict[str, str] = {
    "SaaS / Software": "Information Technology and Services",
    "Fintech": "Financial Services",
    "E-commerce": "Retail",
    "Health Tech": "Hospital & Health Care",
    "Ed Tech": "Education Management",
    "Climate Tech": "Renewables & Environment",
    "Media / Content": "Online Media",
    "Consulting": "Management Consulting",
    "D2C / Consumer": "Consumer Goods",
    "AI / ML": "Artificial Intelligence",
}

_ROLE_EXPANSION: dict[str, list[str]] = {
    "Founder / Co-founder": ["Founder", "Co-Founder", "Co-founder"],
    "Head of Marketing": ["Head of Marketing", "VP Marketing", "VP of Marketing", "Director of Marketing"],
    "VP Sales": ["VP Sales", "VP of Sales", "Head of Sales", "Director of Sales"],
    "Product Manager": ["Product Manager", "Senior Product Manager", "VP Product", "Head of Product"],
    "CTO": ["CTO", "Chief Technology Officer", "VP Engineering", "Head of Engineering"],
    "HR Manager": ["HR Manager", "HR Director", "Head of HR", "Head of People"],
    "Chief of Staff": ["Chief of Staff"],
}


async def search_linkedin_leads_apollo(
    target_role: str,
    locations: list[str],
    industries: list[str],
    keywords: Optional[str] = None,
    limit: int = 50,
    li_at: str = "",
    jsessionid: str = "",
) -> list[dict]:
    """Find leads via Apollo free search + Voyager URL resolution.

    Apollo provides first_name + title + company at zero credit cost.
    Voyager then resolves each person's LinkedIn profile URL by searching
    "{first_name} {company}" and title-matching the best result.
    No Apollo credits are consumed at any point.
    """
    from services.shared.apollo_key_manager import apollo_keys, apollo_post as _apollo_post
    from services.linkedin_outreach.voyager import resolve_linkedin_url
    from core.config import settings

    if not apollo_keys.has_valid_key():
        logger.warning("No valid Apollo API key — skipping Apollo lead search")
        return []
    if not li_at:
        logger.warning("No LinkedIn session — cannot resolve URLs via Voyager, skipping Apollo path")
        return []

    proxy_url = (settings.LINKEDIN_PROXY_URL or "").strip() or None
    person_titles = _ROLE_EXPANSION.get(target_role, [target_role])
    mapped_locations = [_LOCATION_MAP.get(loc, loc) for loc in locations]
    mapped_locations = [l for l in mapped_locations if l]

    _ = industries  # Apollo tag IDs required for industry filter — not used

    # Fetch more candidates than needed; many won't resolve to a URL
    fetch_count = min(limit * 2, 30)
    payload: dict = {
        "person_titles": person_titles,
        "per_page": fetch_count,
        "page": 1,
    }
    if mapped_locations:
        payload["person_locations"] = mapped_locations

    try:
        r = await asyncio.to_thread(
            _apollo_post,
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            json=payload,
            timeout=30,
        )
        if not r.ok:
            logger.warning("Apollo API returned %d: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        raw_people = data.get("people", [])
        logger.info("Apollo free search: %d candidates for role=%r", len(raw_people), target_role)
    except Exception as e:
        logger.error("Apollo search failed: %s", e)
        return []

    people: list[dict] = []
    for p in raw_people:
        if len(people) >= limit:
            break

        first = (p.get("first_name") or "").strip()
        if not first:
            continue

        title = (p.get("title") or "").strip()
        org = p.get("organization") or {}
        company = (org.get("name") or p.get("organization_name") or "").strip()
        if not company:
            continue

        profile_url = await resolve_linkedin_url(
            first_name=first,
            company=company,
            title=title,
            li_at=li_at,
            jsessionid=jsessionid,
            proxy_url=proxy_url,
        )
        if not profile_url:
            continue

        people.append({
            "name": first,
            "headline": title,
            "company": company,
            "profile_url": profile_url,
            "profile_image_url": None,
        })

    logger.info(
        "Apollo+Voyager: resolved %d/%d leads with profile URLs", len(people), len(raw_people)
    )
    return people


# ── Lead search via LinkedIn Voyager (fallback) ────────────────────────────────

def _search_linkedin_leads_sync(
    li_at: str,
    jsessionid: str,
    target_role: str,
    locations: list[str],
    industries: list[str],
    keywords: Optional[str] = None,
    limit: int = 50,
    proxy_url: str = "",
) -> list[dict]:
    """Synchronous people search via LinkedIn GraphQL Voyager endpoint."""
    import re as _re
    import requests
    from urllib.parse import quote

    session = requests.Session()
    session.max_redirects = 3
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}

    keyword_str = f"{target_role} {keywords}".strip() if keywords else target_role
    encoded_kw = quote(keyword_str, safe="")

    variables = (
        f"(start:0,count:{min(limit, 49)},origin:GLOBAL_SEARCH_HEADER,"
        f"query:(keywords:{encoded_kw},flagshipSearchIntent:SEARCH_SRP,"
        f"queryParameters:List((key:resultType,value:List(PEOPLE))),"
        f"includeFiltersInResponse:false))"
    )
    url = (
        "https://www.linkedin.com/voyager/api/graphql"
        f"?includeWebMetadata=true"
        f"&variables={variables}"
        f"&queryId=voyagerSearchDashClusters.b0928897b71bd00a5a7291755dcd64f0"
    )

    headers = {
        "Cookie": f"li_at={li_at}; JSESSIONID={jsessionid}",
        "csrf-token": jsessionid,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.linkedin.com/search/results/people/",
    }

    res = session.get(url, headers=headers, timeout=25, allow_redirects=True)
    logger.info("Lead search status=%d", res.status_code)
    if res.status_code != 200:
        logger.warning("Search returned %d: %s", res.status_code, res.text[:300])
        return []

    data = res.json()
    people = _parse_graphql_search_results(data)
    logger.info("Lead search parsed %d people", len(people))
    return people[:limit]


def _parse_graphql_search_results(data: dict) -> list[dict]:
    import re as _re
    people = []
    clusters = (data.get("data") or {}).get("searchDashClustersByAll") or {}
    for cluster in clusters.get("elements", []):
        for item in cluster.get("items", []):
            entity = ((item.get("item") or {}).get("entityResult")) or {}
            if not entity:
                continue
            name = ((entity.get("title") or {}).get("text") or "").strip()
            if not name:
                continue
            nav_url = entity.get("navigationUrl") or ""
            if "/in/" not in nav_url:
                continue
            profile_url = _re.sub(r"\?.*", "", nav_url).rstrip("/") + "/"
            headline = (
                ((entity.get("primarySubtitle") or {}).get("text") or "")
                or ((entity.get("secondarySubtitle") or {}).get("text") or "")
            )
            people.append({
                "name": name,
                "headline": headline,
                "company": "",
                "profile_url": profile_url,
                "profile_image_url": "",
            })
    return people


async def search_linkedin_leads(
    li_at: str,
    jsessionid: str,
    target_role: str,
    locations: list[str],
    industries: list[str],
    keywords: Optional[str] = None,
    limit: int = 50,
    session_id: str | None = None,
) -> list[dict]:
    """Search for leads. Priority: Voyager role search → Apollo+Voyager resolve → Playwright.

    Voyager role search is tried first because it uses a single API call. The Apollo+Voyager
    path fires one search per Apollo candidate (~90 calls) which quickly triggers rate limiting.
    """
    from core.config import settings
    from services.shared.apollo_key_manager import apollo_keys
    proxy_url = (settings.LINKEDIN_PROXY_URL or "").strip() or None

    # Path 1: Voyager role search — single API call, returns many results directly
    try:
        people = await asyncio.to_thread(
            _search_linkedin_leads_sync,
            li_at=li_at,
            jsessionid=jsessionid,
            target_role=target_role,
            locations=locations,
            industries=industries,
            keywords=keywords,
            limit=limit,
            proxy_url=proxy_url or "",
        )
        if people:
            logger.info("Voyager direct search found %d leads", len(people))
            return people
        logger.info("Voyager direct search returned 0 results — falling back to Apollo+Voyager")
    except Exception as e:
        logger.warning("Voyager direct search failed: %s — falling back to Apollo+Voyager", e)

    # Path 2: Apollo free search → Voyager URL resolution (uses ~15 API calls max)
    if apollo_keys.has_valid_key():
        try:
            people = await search_linkedin_leads_apollo(
                target_role=target_role,
                locations=locations,
                industries=industries,
                keywords=keywords,
                limit=limit,
                li_at=li_at,
                jsessionid=jsessionid,
            )
            if people:
                logger.info("Apollo+Voyager path found %d leads", len(people))
                return people
            logger.info("Apollo+Voyager returned 0 leads — falling back to Playwright")
        except Exception as e:
            logger.warning("Apollo+Voyager path failed: %s — falling back", e)

    if not proxy_url:
        logger.warning("No proxy configured — skipping Playwright lead search fallback")
        return []

    try:
        return await _search_linkedin_leads_playwright(
            li_at=li_at,
            jsessionid=jsessionid,
            target_role=target_role,
            locations=locations,
            industries=industries,
            keywords=keywords,
            limit=limit,
            proxy_url=proxy_url,
        )
    except Exception as e:
        logger.error("Playwright lead search also failed: %s", e)
    return []


async def _search_linkedin_leads_playwright(
    li_at: str,
    jsessionid: str,
    target_role: str,
    locations: list[str],
    industries: list[str],
    keywords: Optional[str] = None,
    limit: int = 30,
    proxy_url: str | None = None,
) -> list[dict]:
    """Browse LinkedIn People search results via headless Chromium to collect profile URLs."""
    from playwright.async_api import async_playwright
    from services.linkedin_outreach.playwright_service import _parse_proxy, _get_fresh_jsessionid
    from urllib.parse import quote

    proxy = _parse_proxy(proxy_url) if proxy_url else None
    fresh = await _get_fresh_jsessionid(li_at, proxy_url)
    if fresh:
        jsessionid = fresh

    keyword_str = " ".join(filter(None, [target_role] + (locations[:1] if locations else []) + ([keywords] if keywords else [])))
    search_url = f"https://www.linkedin.com/search/results/people/?keywords={quote(keyword_str)}&origin=GLOBAL_SEARCH_HEADER"

    profile_urls: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled", "--window-size=1280,900"],
            ignore_default_args=["--enable-automation"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        await ctx.add_cookies([
            {"name": "li_at",      "value": li_at,             "domain": ".linkedin.com", "path": "/"},
            {"name": "JSESSIONID", "value": f'"{jsessionid}"', "domain": ".linkedin.com", "path": "/"},
        ])
        page = await ctx.new_page()

        # Bootstrap session
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        if "/login" in page.url:
            logger.warning("Playwright lead search: session expired")
            await browser.close()
            return []

        await page.goto(search_url, wait_until="networkidle", timeout=40000)
        await page.wait_for_timeout(3000)

        for scroll in range(6):
            links = await page.evaluate("""
                () => {
                    const seen = new Set();
                    return [...document.querySelectorAll('a[href*="/in/"]')]
                        .map(a => {
                            const m = a.href.match(/linkedin\\.com(\\/in\\/[^\\/?#]+)/);
                            return m ? 'https://www.linkedin.com' + m[1] + '/' : null;
                        })
                        .filter(u => {
                            if (!u || seen.has(u)) return false;
                            seen.add(u);
                            return true;
                        });
                }
            """)
            for u in links:
                if u not in profile_urls:
                    profile_urls.append(u)
            logger.info("Lead search scroll %d: %d profiles", scroll + 1, len(profile_urls))
            if len(profile_urls) >= limit * 2:
                break
            await page.evaluate("window.scrollBy(0, 1200)")
            await page.wait_for_timeout(2000)

        await browser.close()

    logger.info("Playwright lead search found %d profiles for %r", len(profile_urls), target_role)

    # Build minimal person dicts — name/headline scraped at send time
    people = []
    for url in profile_urls[:limit]:
        slug = url.rstrip("/").split("/")[-1]
        people.append({
            "profile_url": url,
            "name": slug.replace("-", " ").title(),
            "headline": None,
            "company": None,
            "profile_image_url": None,
        })
    return people


def _parse_search_results(data: dict) -> list[dict]:
    """Parse /voyager/api/search/blended response.

    The response shape is:
      { "included": [...entities...],
        "data": { "elements": [...clusters...] } }

    Each cluster element has a "elements" list of search hits with
    $type == "com.linkedin.voyager.search.SearchProfile".
    We also build an entity map from "included" to look up profile details.
    """
    people = []
    included = data.get("included", [])

    # Build URN → entity lookup for richer profile data
    entity_map: dict[str, dict] = {}
    for entity in included:
        urn = entity.get("entityUrn", "")
        if urn:
            entity_map[urn] = entity

    clusters = data.get("data", {}).get("elements", [])
    for cluster in clusters:
        for hit in cluster.get("elements", []):
            # Each hit has a navigationUrl and may have a profile ref
            hit_type = hit.get("$type", "")
            if "SearchProfile" not in hit_type and "MiniProfile" not in hit_type:
                # Also accept top-level hits that have a publicIdentifier
                pass

            nav_url = hit.get("navigationUrl", "")
            # Resolve the actual profile entity if referenced
            target_urn = hit.get("targetUrn", "") or hit.get("entityUrn", "")
            profile = entity_map.get(target_urn, hit)

            first = profile.get("firstName", {})
            last = profile.get("lastName", {})
            if isinstance(first, dict):
                first = first.get("text", "")
            if isinstance(last, dict):
                last = last.get("text", "")
            name = f"{first} {last}".strip() or hit.get("title", {}).get("text", "")

            headline = profile.get("occupation", "") or hit.get("primarySubtitle", {}).get("text", "")

            # company from included MiniCompany or subtitle
            company = ""
            company_urn = profile.get("currentCompany", "")
            if company_urn and company_urn in entity_map:
                company = entity_map[company_urn].get("name", "")
            if not company:
                company = hit.get("secondarySubtitle", {}).get("text", "")

            public_id = profile.get("publicIdentifier", "")
            if not nav_url and public_id:
                nav_url = f"https://www.linkedin.com/in/{public_id}/"

            if not name or not nav_url:
                continue

            if "/in/" not in nav_url:
                continue

            people.append({
                "name": name,
                "headline": headline,
                "company": company,
                "profile_url": nav_url.split("?")[0],
                "profile_image_url": None,
            })

    return people


# ── Automation daemon ──────────────────────────────────────────────────────────

class LinkedInAutomationDaemon:
    """Background daemon that drives LinkedIn outreach campaigns."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="li-automation")
        self._thread.start()
        logger.info("LinkedIn automation daemon started")

    def stop(self):
        self._stop.set()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while not self._stop.is_set():
            try:
                loop.run_until_complete(self._tick())
            except Exception as e:
                logger.error("LinkedIn daemon tick error: %s", e, exc_info=True)
            self._stop.wait(60)  # run every 60 seconds
        loop.close()

    async def _tick(self):
        from database.session import SessionLocal
        from database.models import LinkedInCampaign, LinkedInConnectionRequest, LinkedInToken
        from services.linkedin_outreach.crypto import decrypt, decrypt_second

        db = SessionLocal()
        try:
            # Get all running campaigns
            campaigns = (
                db.query(LinkedInCampaign)
                .filter(LinkedInCampaign.status == "running")
                .all()
            )

            for campaign in campaigns:
                try:
                    token_row = (
                        db.query(LinkedInToken)
                        .filter(LinkedInToken.user_id == campaign.user_id)
                        .first()
                    )
                    if not token_row:
                        continue

                    # Extension-mode tokens: cookies are bound to the user's home IP,
                    # so server-side sends via proxy will hit a redirect loop. The
                    # extension polls /extension/next-task and runs sends in the browser.
                    if getattr(token_row, "connection_mode", "proxy") == "extension":
                        continue

                    try:
                        li_at = decrypt(token_row.li_at_enc, token_row.nonce)
                        jsessionid = decrypt_second(token_row.jsessionid_enc, token_row.nonce)
                    except Exception:
                        # Decryption failed — key mismatch (e.g. staging vs prod pod) or
                        # token corrupted. Skip silently; the pod that encrypted the token
                        # will process it. Don't mark auth_failed — that status means
                        # LinkedIn session expired, not a local key issue.
                        logger.warning(
                            "Campaign %d: credential decryption failed — skipping this pod's tick",
                            campaign.id,
                        )
                        continue

                    session_id = token_row.proxy_session_id
                    cookies_blob = _decrypt_cookies_blob(token_row)

                    await self._process_campaign(db, campaign, li_at, jsessionid, session_id, cookies_blob)

                except Exception as e:
                    logger.error("Campaign %d processing error: %s", campaign.id, e)

            db.commit()
        finally:
            db.close()

    async def _process_campaign(self, db, campaign, li_at: str, jsessionid: str, session_id: str | None = None, cookies_blob: str | None = None):
        from database.models import LinkedInConnectionRequest

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Count how many we've sent today for this campaign
        sent_today = (
            db.query(LinkedInConnectionRequest)
            .filter(
                LinkedInConnectionRequest.campaign_id == campaign.id,
                LinkedInConnectionRequest.sent_at >= today_start,
                LinkedInConnectionRequest.status.in_(["sent", "accepted", "followup_sent", "replied"]),
            )
            .count()
        )

        remaining_today = campaign.daily_limit - sent_today

        # Phase 1: send pending connection requests (IST business hours only)
        if remaining_today > 0 and _is_ist_sending_window():
            pending = (
                db.query(LinkedInConnectionRequest)
                .filter(
                    LinkedInConnectionRequest.campaign_id == campaign.id,
                    LinkedInConnectionRequest.status.in_(["pending", "error"]),
                )
                .limit(min(remaining_today, 5))  # max 5 per daemon tick
                .all()
            )

            first_in_batch = True
            for req in pending:
                # Sleep between sends but NOT before the very first attempt —
                # auth failures surface immediately on the first tick.
                if not first_in_batch:
                    await asyncio.sleep(random.uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS))
                first_in_batch = False

                # Resolve profile_url via Voyager if not already known.
                # Apollo search doesn't return linkedin_url for most leads, so
                # we search by name + company just before sending.
                if not req.profile_url:
                    try:
                        from services.linkedin_outreach.voyager import resolve_linkedin_url
                        from core.config import settings as _settings
                        _proxy_url = (_settings.LINKEDIN_PROXY_URL or "").strip() or None
                        first_name = (req.name or "").split()[0] if req.name else ""
                        resolved = await resolve_linkedin_url(
                            first_name, req.company or "", req.headline or "",
                            li_at, jsessionid,
                            proxy_url=_proxy_url,
                        )
                        if resolved:
                            req.profile_url = resolved
                            db.commit()
                        else:
                            logger.warning("Campaign %d: could not resolve LinkedIn URL for '%s' at '%s', skipping", campaign.id, req.name, req.company)
                            req.status = "error"
                            req.error = "Could not resolve LinkedIn profile"
                            req.updated_at = datetime.utcnow()
                            db.commit()
                            continue
                    except Exception as resolve_err:
                        logger.warning("Campaign %d: Voyager resolve error for '%s': %s", campaign.id, req.name, resolve_err)
                        req.status = "error"
                        req.error = "Profile resolve failed"
                        req.updated_at = datetime.utcnow()
                        db.commit()
                        continue

                # Resolve URN best-effort (Playwright fallback works without it).
                if not req.profile_urn:
                    try:
                        urn = await resolve_profile_urn(li_at, jsessionid, req.profile_url, session_id, cookies_blob)
                    except LinkedInAuthError:
                        logger.warning("Campaign %d: auth failed during URN resolve, marking auth_failed", campaign.id)
                        campaign.status = "auth_failed"
                        campaign.updated_at = datetime.utcnow()
                        db.commit()
                        return
                    if urn:
                        req.profile_urn = urn

                # Send — pass profile_url so Playwright works even if URN is empty
                try:
                    ok = await send_connection_request(
                        li_at, jsessionid,
                        req.profile_urn or "",
                        req.connection_note or "",
                        session_id,
                        profile_url=req.profile_url,
                        cookies_blob=cookies_blob,
                    )
                except LinkedInAuthError:
                    logger.warning("Campaign %d: LinkedIn session expired, marking auth_failed", campaign.id)
                    campaign.status = "auth_failed"
                    campaign.updated_at = datetime.utcnow()
                    db.commit()
                    return
                except Exception as send_err:
                    logger.warning("Campaign %d: send error for %s: %s", campaign.id, req.profile_url, send_err)
                    ok = False

                if ok:
                    req.status = "sent"
                    req.sent_at = datetime.utcnow()
                    campaign.total_sent += 1
                else:
                    req.status = "error"
                    req.error = "Send failed"
                req.updated_at = datetime.utcnow()
                db.commit()
        elif remaining_today > 0:
            logger.debug("Campaign %d: outside IST sending window, skipping Phase 1", campaign.id)

        # Phase 2: check accepted connections → send follow-up (every 4h, independent timer)
        now_ts = _time.monotonic()
        should_check_accepted = (
            now_ts - _last_accept_check.get(campaign.id, 0) >= _ACCEPT_INTERVAL
            or (campaign.total_sent > 0 and campaign.total_accepted == 0)
        )

        if should_check_accepted:
            _last_accept_check[campaign.id] = now_ts
            sent_requests = (
                db.query(LinkedInConnectionRequest)
                .filter(
                    LinkedInConnectionRequest.campaign_id == campaign.id,
                    LinkedInConnectionRequest.status == "sent",
                    LinkedInConnectionRequest.sent_at >= datetime.utcnow() - timedelta(days=14),
                )
                .limit(20)
                .all()
            )

            for req in sent_requests:
                if not req.profile_urn:
                    continue
                try:
                    accepted = await check_connection_accepted(li_at, jsessionid, req.profile_urn, session_id)
                except Exception as e:
                    logger.warning("Campaign %d: acceptance check error for %s: %s", campaign.id, req.profile_urn, e)
                    continue
                if accepted:
                    req.status = "accepted"
                    req.accepted_at = datetime.utcnow()
                    campaign.total_accepted += 1
                    db.commit()

                    # Send follow-up 2–3 minutes after acceptance (human-like delay)
                    if campaign.followup_message and req.followup_message:
                        await asyncio.sleep(random.uniform(120, 180))
                        ok = await send_message(
                            li_at, jsessionid, req.profile_urn, req.followup_message, session_id
                        )
                        if ok:
                            req.status = "followup_sent"
                            req.followup_sent_at = datetime.utcnow()
                            campaign.total_followups_sent += 1
                            db.commit()

        # Phase 3: detect replies — independent 2h timer so it runs even when Phase 2 is cooling down
        should_check_replies = (now_ts - _last_reply_check.get(campaign.id, 0) >= _REPLY_INTERVAL)

        followup_reqs = (
            db.query(LinkedInConnectionRequest)
            .filter(
                LinkedInConnectionRequest.campaign_id == campaign.id,
                LinkedInConnectionRequest.status == "followup_sent",
                LinkedInConnectionRequest.profile_urn.isnot(None),
            )
            .limit(20)
            .all()
        )

        if should_check_replies and followup_reqs:
            _last_reply_check[campaign.id] = now_ts
            urn_to_req = {r.profile_urn: r for r in followup_reqs}
            try:
                async with httpx.AsyncClient(timeout=20, **_proxy(session_id)) as client:
                    inbox_res = await client.get(
                        "https://www.linkedin.com/voyager/api/messaging/conversations"
                        "?keyVersion=LEGACY_INBOX&q=inbox&count=20",
                        headers=_headers(li_at, jsessionid),
                    )
                if inbox_res.status_code == 200:
                    data = inbox_res.json()
                    # Voyager normalises: conversations are in data.elements,
                    # and in some response shapes directly in included.
                    convs = (
                        data.get("data", {}).get("elements", [])
                        or data.get("included", [])
                    )
                    for conv in convs:
                        last_ms = conv.get("lastActivityAt", 0)
                        if not last_ms:
                            continue
                        last_dt = datetime.utcfromtimestamp(last_ms / 1000)

                        # Participants come as a list of URN strings or reference objects
                        participants = conv.get("participants") or conv.get("*participants") or []
                        for p_ref in participants:
                            p_str = str(p_ref)
                            if "fsd_profile:" in p_str:
                                purn = p_str.split("fsd_profile:")[-1].split(")")[0].strip().strip('"')
                            else:
                                purn = p_str.strip().strip('"')
                            req = urn_to_req.get(purn)
                            if req and req.followup_sent_at and last_dt > req.followup_sent_at:
                                req.status = "replied"
                                req.reply_received_at = last_dt
                                campaign.total_replied += 1
                                db.commit()
                                break
            except Exception as e:
                logger.warning("Campaign %d: reply check failed: %s", campaign.id, e)

        campaign.updated_at = datetime.utcnow()


# Singleton daemon
_daemon = LinkedInAutomationDaemon()


def start_automation_daemon():
    _daemon.start()


def stop_automation_daemon():
    _daemon.stop()
