"""LinkedIn outreach automation — background daemon for connection requests + follow-ups."""

import asyncio
import logging
import random
import re
import threading
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


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


# ── Voyager helpers ────────────────────────────────────────────────────────────

def _headers(li_at: str, jsessionid: str) -> dict:
    return {
        "Cookie": f"li_at={li_at}; JSESSIONID={jsessionid}",
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


async def resolve_profile_urn(li_at: str, jsessionid: str, profile_url: str, session_id: str | None = None) -> Optional[str]:
    """Extract fsd_profile URN from a LinkedIn profile URL.

    Raises LinkedInAuthError if the session is expired (401/403 or redirect to login page).
    """
    slug = profile_url.rstrip("/").split("/in/")[-1].split("?")[0].split("/")[0]
    url = f"https://www.linkedin.com/in/{slug}/"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, **_proxy(session_id)) as client:
            res = await client.get(
                url,
                headers={**_headers(li_at, jsessionid), "Accept": "text/html"},
            )
        if res.status_code in (401, 403):
            raise LinkedInAuthError(f"Session expired (status {res.status_code}) resolving {profile_url}")
        # Redirect to login page indicates expired session even with follow_redirects=True
        if "/login" in str(res.url) or "/uas/login" in str(res.url):
            raise LinkedInAuthError("Session expired (redirected to login page)")
        matches = re.findall(r"fsd_profile:([A-Za-z0-9_-]{30,})", res.text)
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
) -> bool:
    """Send a LinkedIn connection request via Voyager. Returns True on success.

    Raises LinkedInAuthError when the session is expired (401/403).
    """
    payload = {
        "trackingId": _tracking_id(),
        "invitee": {
            "com.linkedin.voyager.relationships.invitation.InviteeProfile": {
                "profileId": profile_urn,
            }
        },
        "customMessage": note[:300] if note else "",
    }

    logger.info("send_connection_request profileId=%s payload=%s", profile_urn, payload)
    try:
        async with httpx.AsyncClient(timeout=20, **_proxy(session_id)) as client:
            res = await client.post(
                "https://www.linkedin.com/voyager/api/relationships/invitations",
                headers=_headers(li_at, jsessionid),
                json=payload,
            )
        logger.info("invitations response %d: %s", res.status_code, res.text[:500])
        if res.status_code in (200, 201):
            return True
        if res.status_code in (401, 403):
            raise LinkedInAuthError(f"Session expired (status {res.status_code})")
        logger.warning("Connection request got status %d: %s", res.status_code, res.text[:500])
        return False
    except LinkedInAuthError:
        raise
    except Exception as e:
        logger.error("send_connection_request failed: %s", e)
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


def _apollo_match_by_id(api_key: str, person_id: str) -> Optional[str]:
    """Fetch LinkedIn URL for a single Apollo person ID. Free — no credits consumed."""
    import requests as _req
    try:
        r = _req.post(
            "https://api.apollo.io/api/v1/people/match",
            json={"id": person_id},
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": api_key,
            },
            timeout=15,
        )
        if not r.ok:
            return None
        p = r.json().get("person") or {}
        url = (p.get("linkedin_url") or "").strip()
        return url if "/in/" in url else None
    except Exception:
        return None


async def search_linkedin_leads_apollo(
    target_role: str,
    locations: list[str],
    industries: list[str],
    keywords: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Find leads via Apollo.io people search. Does not require LinkedIn session."""
    import requests as _req
    try:
        from core.config import settings
        api_key = (settings.APOLLO_API_KEY or "").strip()
    except Exception:
        api_key = ""

    if not api_key or api_key == "your_apollo_key_here":
        logger.warning("APOLLO_API_KEY not configured — skipping Apollo lead search")
        return []

    person_titles = _ROLE_EXPANSION.get(target_role, [target_role])

    mapped_locations = [_LOCATION_MAP.get(loc, loc) for loc in locations]
    mapped_locations = [l for l in mapped_locations if l]  # drop blanks (Global)

    # Apollo's organization_industries field requires internal tag IDs, not strings —
    # string values always return 0 results. Filter by role + location only; the
    # enrichment step (people/match) returns enough signal for targeting.
    _ = industries  # kept in signature for API compatibility

    # Fetch 2× the needed count — match calls filter out those without LinkedIn URLs
    fetch_count = min(limit * 2, 60)
    payload: dict = {
        "person_titles": person_titles,
        "per_page": fetch_count,
        "page": 1,
    }
    if mapped_locations:
        payload["person_locations"] = mapped_locations

    try:
        r = await asyncio.to_thread(
            _req.post,
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": api_key,
            },
            timeout=30,
        )
        if not r.ok:
            logger.warning("Apollo API returned %d: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        raw_people = data.get("people", [])
        logger.info("Apollo search: %d raw candidates for role=%r", len(raw_people), target_role)

        # Step 2: enrich each person by ID to get their LinkedIn URL (free, no credits)
        people = []
        sem = asyncio.Semaphore(10)  # max 10 concurrent match calls

        async def enrich(p: dict) -> Optional[dict]:
            person_id = p.get("id")
            if not person_id:
                return None
            first = p.get("first_name") or ""
            org = p.get("organization") or {}
            company = org.get("name", "") if isinstance(org, dict) else ""
            async with sem:
                linkedin_url = await asyncio.to_thread(_apollo_match_by_id, api_key, person_id)
            if not linkedin_url:
                return None
            # Use matched data: might have full last name in match response
            name = first.strip()
            if not name:
                return None
            return {
                "name": name,
                "headline": p.get("title") or "",
                "company": company,
                "profile_url": linkedin_url.rstrip("/") + "/",
                "profile_image_url": p.get("photo_url"),
            }

        results = await asyncio.gather(*[enrich(p) for p in raw_people])
        people = [r for r in results if r is not None][:limit]
        logger.info("Apollo search: %d leads with LinkedIn URLs from %d candidates", len(people), len(raw_people))
        return people
    except Exception as e:
        logger.error("Apollo search failed: %s", e)
        return []


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
    """Synchronous search using a plain requests.Session routed via residential proxy.

    We use requests (not httpx) because requests preserves Voyager filter
    syntax literally, while httpx encodes -> as %3E. Routed through the
    Evomi residential proxy so LinkedIn doesn't reject the datacenter IP.
    """
    import requests

    session = requests.Session()
    session.max_redirects = 3
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}

    keyword_str = f"{target_role} {keywords}".strip() if keywords else target_role

    # Use (field:X,values:List(Y)) syntax — no -> characters that requests would encode as %3E
    filter_parts = ["(field:resultType,values:List(PEOPLE))"]
    if locations:
        loc_vals = ",".join(loc for loc in locations[:3])
        filter_parts.append(f"(field:geoUrn,values:List({loc_vals}))")
    if industries:
        ind_vals = ",".join(i for i in industries[:3])
        filter_parts.append(f"(field:industry,values:List({ind_vals}))")

    params = {
        "count": min(limit, 49),
        "filters": f"List({','.join(filter_parts)})",
        "keywords": keyword_str,
        "origin": "GLOBAL_SEARCH_HEADER",
        "q": "all",
        "start": 0,
        "queryContext": "List(spellCorrectionEnabled->true,relatedSearchesEnabled->true)",
    }

    # JSESSIONID must match csrf-token exactly — no quotes around it in Cookie header
    headers = {
        "Cookie": f"li_at={li_at}; JSESSIONID={jsessionid}",
        "csrf-token": jsessionid,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.linkedin.com/search/results/people/",
    }

    url = "https://www.linkedin.com/voyager/api/search/blended"
    res = session.get(url, params=params, headers=headers, timeout=25, allow_redirects=True)
    logger.info("Lead search status=%d", res.status_code)
    if res.status_code != 200:
        logger.warning("Search returned %d: %s", res.status_code, res.text[:300])
        return []

    data = res.json()
    people = _parse_search_results(data)
    logger.info("Lead search parsed %d people", len(people))
    return people[:limit]


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
    """Search for leads. Tries Apollo.io first (no session needed), falls back to LinkedIn Voyager."""
    # Apollo path — preferred, no JSESSIONID required
    people = await search_linkedin_leads_apollo(target_role, locations, industries, keywords, limit)
    if people:
        return people

    logger.warning("Apollo returned 0 leads; falling back to LinkedIn Voyager")
    try:
        from core.config import settings
        proxy_url = settings.LINKEDIN_PROXY_URL.strip() if settings.LINKEDIN_PROXY_URL else ""
        people = await asyncio.to_thread(
            _search_linkedin_leads_sync,
            li_at, jsessionid, target_role, locations, industries, keywords, limit, proxy_url,
        )
        return people
    except Exception as e:
        logger.error("Voyager search failed: %s", e)
        return []


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
        from services.linkedin_outreach.crypto import decrypt

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

                    try:
                        li_at = decrypt(token_row.li_at_enc, token_row.nonce)
                        jsessionid = decrypt(token_row.jsessionid_enc, token_row.nonce)
                    except Exception:
                        # Decryption failed — key rotated or token corrupted; tell user to reconnect
                        logger.warning(
                            "Campaign %d: credential decryption failed, marking auth_failed",
                            campaign.id,
                        )
                        campaign.status = "auth_failed"
                        campaign.updated_at = datetime.utcnow()
                        db.commit()
                        continue

                    session_id = token_row.proxy_session_id

                    await self._process_campaign(db, campaign, li_at, jsessionid, session_id)

                except Exception as e:
                    logger.error("Campaign %d processing error: %s", campaign.id, e)

            db.commit()
        finally:
            db.close()

    async def _process_campaign(self, db, campaign, li_at: str, jsessionid: str, session_id: str | None = None):
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

        # Phase 1: send pending connection requests
        if remaining_today > 0:
            pending = (
                db.query(LinkedInConnectionRequest)
                .filter(
                    LinkedInConnectionRequest.campaign_id == campaign.id,
                    LinkedInConnectionRequest.status == "pending",
                )
                .limit(min(remaining_today, 5))  # max 5 per daemon tick
                .all()
            )

            first_in_batch = True
            for req in pending:
                # Sleep between sends but NOT before the very first attempt —
                # this way auth failures surface immediately on the first tick.
                if not first_in_batch:
                    await asyncio.sleep(random.uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS))
                first_in_batch = False

                # Resolve URN if we don't have it
                if not req.profile_urn:
                    try:
                        urn = await resolve_profile_urn(li_at, jsessionid, req.profile_url, session_id)
                    except LinkedInAuthError:
                        logger.warning("Campaign %d: auth failed during URN resolve, marking auth_failed", campaign.id)
                        campaign.status = "auth_failed"
                        campaign.updated_at = datetime.utcnow()
                        db.commit()
                        return
                    if urn:
                        req.profile_urn = urn
                    else:
                        req.status = "error"
                        req.error = "Could not resolve profile URN"
                        req.updated_at = datetime.utcnow()
                        db.commit()
                        continue

                try:
                    ok = await send_connection_request(
                        li_at, jsessionid, req.profile_urn, req.connection_note or "", session_id
                    )
                except LinkedInAuthError:
                    # Session expired — pause campaign so user knows to reconnect
                    logger.warning("Campaign %d: LinkedIn session expired, pausing", campaign.id)
                    campaign.status = "auth_failed"
                    campaign.updated_at = datetime.utcnow()
                    db.commit()
                    return
                if ok:
                    req.status = "sent"
                    req.sent_at = datetime.utcnow()
                    campaign.total_sent += 1
                else:
                    req.status = "error"
                    req.error = "Send failed"
                req.updated_at = datetime.utcnow()
                db.commit()

        # Phase 2: check for accepted connections → send follow-up
        sent_requests = (
            db.query(LinkedInConnectionRequest)
            .filter(
                LinkedInConnectionRequest.campaign_id == campaign.id,
                LinkedInConnectionRequest.status == "sent",
                LinkedInConnectionRequest.sent_at >= datetime.utcnow() - timedelta(days=14),
            )
            .limit(10)
            .all()
        )

        for req in sent_requests:
            if not req.profile_urn:
                continue
            accepted = await check_connection_accepted(li_at, jsessionid, req.profile_urn, session_id)
            if accepted:
                req.status = "accepted"
                req.accepted_at = datetime.utcnow()
                campaign.total_accepted += 1
                db.commit()

                # Send follow-up if configured
                if campaign.followup_message and req.followup_message:
                    await asyncio.sleep(random.uniform(30, 120))
                    ok = await send_message(
                        li_at, jsessionid, req.profile_urn, req.followup_message, session_id
                    )
                    if ok:
                        req.status = "followup_sent"
                        req.followup_sent_at = datetime.utcnow()
                        campaign.total_followups_sent += 1
                        db.commit()

        campaign.updated_at = datetime.utcnow()


# Singleton daemon
_daemon = LinkedInAutomationDaemon()


def start_automation_daemon():
    _daemon.start()


def stop_automation_daemon():
    _daemon.stop()
