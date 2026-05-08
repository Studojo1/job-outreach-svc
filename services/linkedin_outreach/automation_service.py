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

    If session_id is provided, uses a sticky session so all requests for
    a given LinkedIn account come from the same residential IP.
    Format: http://user-session-ID:pass@core-residential.evomi.com:1000
    """
    from core.config import settings
    base_url = settings.LINKEDIN_PROXY_URL.strip()
    if not base_url:
        return {}
    if session_id:
        # Insert -session-ID into the username part
        # base_url format: http://user:pass@host:port
        try:
            from urllib.parse import urlparse, urlunparse
            p = urlparse(base_url)
            sticky_netloc = p.netloc.replace(
                p.username, f"{p.username}-session-{session_id}", 1
            )
            base_url = urlunparse(p._replace(netloc=sticky_netloc))
        except Exception:
            pass
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
    """Extract fsd_profile URN from a LinkedIn profile URL."""
    slug = profile_url.rstrip("/").split("/in/")[-1].split("?")[0].split("/")[0]
    url = f"https://www.linkedin.com/in/{slug}/"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, **_proxy(session_id)) as client:
            res = await client.get(
                url,
                headers={**_headers(li_at, jsessionid), "Accept": "text/html"},
            )
        matches = re.findall(r"fsd_profile:([A-Za-z0-9_-]{30,})", res.text)
        for m in matches:
            if m != "urn":
                return m
    except Exception as e:
        logger.warning("URN resolve failed for %s: %s", profile_url, e)
    return None


async def send_connection_request(
    li_at: str,
    jsessionid: str,
    profile_urn: str,
    note: str = "",
    session_id: str | None = None,
) -> bool:
    """Send a LinkedIn connection request via Voyager. Returns True on success."""
    payload = {
        "trackingId": _tracking_id(),
        "invitations": [],
        "excludeInvitations": [],
        "invitee": {
            "com.linkedin.voyager.growth.invitation.InviteeProfile": {
                "profileId": profile_urn,
            }
        },
    }
    if note:
        payload["message"] = note[:300]

    try:
        async with httpx.AsyncClient(timeout=20, **_proxy(session_id)) as client:
            res = await client.post(
                "https://www.linkedin.com/voyager/api/growth/normInvitations",
                headers=_headers(li_at, jsessionid),
                json=payload,
            )
        if res.status_code in (200, 201):
            return True
        logger.warning("Connection request got status %d: %s", res.status_code, res.text[:200])
        return False
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


# ── Lead search via Voyager ────────────────────────────────────────────────────

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
    """Search LinkedIn for people via the stable Voyager REST blended-search endpoint.

    Uses /voyager/api/search/blended (same endpoint as linkedin-api library) instead
    of the GraphQL endpoint whose queryId hash changes with every LinkedIn deploy.
    """
    keyword_str = target_role
    if keywords:
        keyword_str = f"{target_role} {keywords}"

    filter_parts = ["resultType->List(PEOPLE)"]
    if locations:
        locs = ",".join(f"(text:{loc})" for loc in locations[:3])
        filter_parts.append(f"geoUrn->List({locs})")
    if industries:
        inds = ",".join(f"(text:{i})" for i in industries[:3])
        filter_parts.append(f"industry->List({inds})")

    filters_str = f"List({','.join(filter_parts)})"

    params = {
        "count": min(limit, 49),
        "filters": filters_str,
        "keywords": keyword_str,
        "origin": "GLOBAL_SEARCH_HEADER",
        "q": "all",
        "start": 0,
        "queryContext": "List(spellCorrectionEnabled->true,relatedSearchesEnabled->true)",
    }

    from urllib.parse import urlencode
    url = "https://www.linkedin.com/voyager/api/search/blended?" + urlencode(params)

    people = []
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, **_proxy(session_id)) as client:
            res = await client.get(url, headers=_headers(li_at, jsessionid))
        logger.info("Lead search status=%d url=%s", res.status_code, url)
        if res.status_code != 200:
            logger.warning("Search returned %d: %s", res.status_code, res.text[:300])
            return []
        data = res.json()
        people = _parse_search_results(data)
        logger.info("Lead search parsed %d people", len(people))
    except Exception as e:
        logger.error("search_linkedin_leads failed: %s", e)

    return people[:limit]


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

                    li_at = decrypt(token_row.li_at_enc, token_row.nonce)
                    jsessionid = decrypt(token_row.jsessionid_enc, token_row.nonce)
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

            for req in pending:
                await asyncio.sleep(random.uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS))

                # Resolve URN if we don't have it
                if not req.profile_urn:
                    urn = await resolve_profile_urn(li_at, jsessionid, req.profile_url, session_id)
                    if urn:
                        req.profile_urn = urn
                    else:
                        req.status = "error"
                        req.error = "Could not resolve profile URN"
                        req.updated_at = datetime.utcnow()
                        db.commit()
                        continue

                ok = await send_connection_request(
                    li_at, jsessionid, req.profile_urn, req.connection_note or "", session_id
                )
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
