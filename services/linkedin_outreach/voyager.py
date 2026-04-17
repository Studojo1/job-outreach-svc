"""LinkedIn Voyager API client — authenticated people search and messaging using li_at session."""

import asyncio
import json
import logging
import random
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# LinkedIn Voyager API base
VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

# Mimick a real Chrome browser on macOS
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

_LI_TRACK = (
    '{"clientVersion":"1.13.1862","mpVersion":"1.13.1862","osName":"web",'
    '"timezoneOffset":5.5,"timezone":"Asia/Calcutta","deviceFormFactor":"DESKTOP",'
    '"mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}'
)


def _build_headers(li_at: str, jsessionid: str) -> dict:
    return {
        "Cookie": f"li_at={li_at}; JSESSIONID={jsessionid}",
        "csrf-token": jsessionid,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "x-li-track": _LI_TRACK,
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.linkedin.com/search/results/people/",
        "Origin": "https://www.linkedin.com",
    }


def _parse_search_results(data: dict) -> list[dict]:
    """Parse Voyager blended search response into a flat list of people dicts."""
    people = []

    # Voyager returns a normalised JSON: top-level 'data' + 'included' array
    included = data.get("included", [])

    # Build a lookup of all included entities by their $type + entityUrn
    entity_map: dict[str, dict] = {}
    for entity in included:
        urn = entity.get("entityUrn", "")
        if urn:
            entity_map[urn] = entity

    # The search results are under data.elements[].elements[]
    elements = (
        data.get("data", {})
        .get("elements", [])
    )

    for cluster in elements:
        for item in cluster.get("elements", []):
            # Each item may have a trackingUrn pointing to the member
            member_urn = item.get("targetUrn") or item.get("entityUrn", "")

            # Try to find the profile entity
            profile = entity_map.get(member_urn, {})

            # publicIdentifier is the LinkedIn slug (e.g. "john-doe-12345")
            public_id = profile.get("publicIdentifier") or item.get("publicIdentifier")
            if not public_id:
                continue

            name = profile.get("firstName", "") + " " + profile.get("lastName", "")
            name = name.strip()

            headline = profile.get("headline", "")

            # Company: look for miniProfile's occupation or headline
            company = ""
            occupation = profile.get("occupation", "")
            if occupation:
                company = occupation

            # Profile image
            image_url = ""
            picture = profile.get("picture", {})
            if picture:
                artifacts = picture.get("com.linkedin.common.VectorImage", {}).get("artifacts", [])
                if artifacts:
                    root = picture.get("com.linkedin.common.VectorImage", {}).get("rootUrl", "")
                    last = artifacts[-1].get("fileIdentifyingUrlPathSegment", "")
                    if root and last:
                        image_url = root + last

            people.append({
                "name": name or "Unknown",
                "headline": headline,
                "company": company,
                "profile_url": f"https://www.linkedin.com/in/{public_id}/",
                "profile_image_url": image_url,
            })

    return people


async def search_people(
    li_at: str,
    jsessionid: str,
    keywords: str,
    location: Optional[str] = None,
    count: int = 10,
) -> list[dict]:
    """Search LinkedIn for people matching keywords.

    Returns a list of dicts with: name, headline, company, profile_url, profile_image_url.
    Raises httpx.HTTPStatusError on auth failure or rate limit.
    """
    # Random human-like delay
    await asyncio.sleep(random.uniform(1.5, 3.0))

    params: dict = {
        "keywords": keywords,
        "q": "all",
        "filters": "List(resultType->PEOPLE)",
        "origin": "SWITCH_SEARCH_VERTICAL",
        "count": count,
        "start": 0,
    }

    if location:
        # LinkedIn geo IDs: India=102713980, US=103644278, UK=101165590
        # For MVP we pass location as a keyword filter
        params["keywords"] = f"{keywords} {location}"

    headers = _build_headers(li_at, jsessionid)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
    ) as client:
        resp = await client.get(
            f"{VOYAGER_BASE}/search/blended",
            params=params,
            headers=headers,
        )

    if resp.status_code == 401 or resp.status_code == 403:
        logger.warning("LinkedIn auth failed (status %s) — token may be expired", resp.status_code)
        raise ValueError("LinkedIn session expired. Please reconnect via the extension.")

    if resp.status_code == 429:
        logger.warning("LinkedIn rate limited")
        raise ValueError("LinkedIn rate limited. Please wait a few minutes and try again.")

    resp.raise_for_status()

    try:
        data = resp.json()
    except Exception as e:
        logger.error("Failed to parse LinkedIn response: %s", e)
        return []

    people = _parse_search_results(data)
    logger.info("LinkedIn search '%s' returned %d people", keywords, len(people))
    return people


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_slug(profile_url: str) -> str:
    """Extract the LinkedIn public identifier (slug) from any profile URL or bare slug."""
    url = profile_url.strip().rstrip("/")
    if "/in/" in url:
        return url.split("/in/")[-1].split("/")[0].split("?")[0]
    return url.split("/")[-1].split("?")[0]


def _auth_check(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise ValueError("LinkedIn session expired. Reconnect via the extension.")
    if resp.status_code == 429:
        raise ValueError("LinkedIn rate limited. Wait a few minutes and try again.")
    resp.raise_for_status()


def _extract_urn_id(urn: str) -> str:
    """Pull the ID fragment from any LinkedIn URN."""
    if urn and ":" in urn:
        fragment = urn.rsplit(":", 1)[-1]
        if len(fragment) > 4:
            return fragment
    return ""


async def _resolve_member_urns(
    client: httpx.AsyncClient,
    headers: dict,
    slug: str,
) -> tuple[str, str]:
    """Return (member_numeric_id, fsd_profile_id) for a LinkedIn slug.

    member_numeric_id is a plain integer string like "123456789" — used as
    urn:li:member:{id} which is the most compatible recipient format for messaging.
    fsd_profile_id is the base64 token from urn:li:fsd_profile:{id}.

    Either value may be empty string if not found via that strategy.
    """
    member_id = ""
    fsd_id = ""
    ref_headers = {**headers, "Referer": f"https://www.linkedin.com/in/{slug}/"}

    # ── Strategy 1: fetch the profile page HTML and regex out URNs ────────────
    # The rendered page reliably embeds urn:li:member:{numericId} in the JSON blobs.
    try:
        page_resp = await client.get(
            f"https://www.linkedin.com/in/{slug}/",
            headers={
                "Cookie": headers["Cookie"],
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.linkedin.com/feed/",
            },
        )
        if page_resp.status_code == 200:
            html = page_resp.text
            # Numeric member ID — most useful for messaging
            m = re.search(r'"objectUrn"\s*:\s*"urn:li:member:(\d+)"', html)
            if not m:
                m = re.search(r'urn:li:member:(\d+)', html)
            if m:
                member_id = m.group(1)
            # fsd_profile token as fallback
            f = re.search(r'urn:li:fsd_profile:([A-Za-z0-9_-]{10,})', html)
            if f:
                fsd_id = f.group(1)
    except Exception:
        pass

    if member_id or fsd_id:
        return member_id, fsd_id

    # ── Strategy 2: dash profiles API ─────────────────────────────────────────
    try:
        resp = await client.get(
            f"{VOYAGER_BASE}/identity/dash/profiles",
            params={"q": "memberIdentity", "memberIdentity": slug},
            headers=ref_headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            all_entities = data.get("included", []) + (
                data.get("data", {})
                    .get("identityDashProfilesByMemberIdentity", {})
                    .get("elements", [])
            )
            for entity in all_entities:
                obj_urn = entity.get("objectUrn", "")
                if obj_urn and "member:" in obj_urn:
                    m = re.search(r'member:(\d+)', obj_urn)
                    if m:
                        member_id = m.group(1)
                urn = entity.get("entityUrn", "")
                if "fsd_profile" in urn and not fsd_id:
                    fsd_id = _extract_urn_id(urn)
            if member_id or fsd_id:
                return member_id, fsd_id
    except Exception:
        pass

    # ── Strategy 3: basic identity/profiles ───────────────────────────────────
    try:
        resp = await client.get(
            f"{VOYAGER_BASE}/identity/profiles/{slug}",
            headers=ref_headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            all_entities = [data] + data.get("included", [])
            for entity in all_entities:
                for key in ("objectUrn", "entityUrn"):
                    urn = entity.get(key, "")
                    if "member:" in urn:
                        m = re.search(r'member:(\d+)', urn)
                        if m:
                            member_id = m.group(1)
                    if "fsd_profile" in urn and not fsd_id:
                        fsd_id = _extract_urn_id(urn)
                    if "fs_miniProfile" in urn and not fsd_id:
                        fsd_id = _extract_urn_id(urn)
            if member_id or fsd_id:
                return member_id, fsd_id
    except Exception:
        pass

    raise ValueError(
        f"Could not resolve LinkedIn profile '{slug}'. "
        "Ensure the URL is correct and the account is logged into LinkedIn."
    )


# ── Message sending ───────────────────────────────────────────────────────────

async def send_linkedin_message(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    content: str,
) -> dict:
    """Send a direct message to a LinkedIn member via Voyager API."""
    slug = _extract_slug(profile_url)
    headers = _build_headers(li_at, jsessionid)
    headers["Content-Type"] = "application/json"

    await asyncio.sleep(random.uniform(1.0, 2.0))

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0)) as client:
        member_id, fsd_id = await _resolve_member_urns(client, headers, slug)

        # Build candidate recipient URNs — numeric member URN is most reliable
        recipients: list[str] = []
        if member_id:
            recipients.append(f"urn:li:member:{member_id}")
        if fsd_id:
            recipients.append(f"urn:li:fsd_profile:{fsd_id}")
            recipients.append(f"urn:li:fs_miniProfile:{fsd_id}")

        if not recipients:
            raise ValueError(f"Could not resolve any URN for '{slug}'")

        last_err: Exception | None = None
        for recipient in recipients:
            payload = {
                "keyVersion": "LEGACY_INBOX",
                "conversationCreate": {
                    "eventCreate": {
                        "value": {
                            "com.linkedin.voyager.messaging.create.MessageCreate": {
                                "attributedBody": {"text": content, "attributes": []},
                                "attachments": [],
                            }
                        }
                    },
                    "recipients": [recipient],
                    "subtype": "MEMBER_TO_MEMBER",
                },
            }
            try:
                resp = await client.post(
                    f"{VOYAGER_BASE}/messaging/conversations",
                    content=json.dumps(payload),
                    headers=headers,
                )
                if resp.status_code in (200, 201):
                    logger.info("Message sent to %s via %s", slug, recipient)
                    return {"ok": True}
                last_err = Exception(
                    f"LinkedIn {resp.status_code} with {recipient}: {resp.text[:300]}"
                )
            except Exception as e:
                last_err = e

        raise ValueError(str(last_err))


# ── Connection request ────────────────────────────────────────────────────────

async def send_linkedin_connection(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    note: str = "",
) -> dict:
    """Send a LinkedIn connection request, optionally with a personalised note."""
    slug = _extract_slug(profile_url)
    headers = _build_headers(li_at, jsessionid)
    headers["Content-Type"] = "application/json"

    await asyncio.sleep(random.uniform(1.0, 2.0))

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0)) as client:
        member_id, fsd_id = await _resolve_member_urns(client, headers, slug)

        last_err: Exception | None = None
        attempts: list[dict] = []

        if fsd_id:
            attempts.append({
                "invitee": {
                    "com.linkedin.voyager.growth.invitation.InviteeProfile": {
                        "profileId": fsd_id,
                    }
                },
                **({"customMessage": note[:300]} if note else {}),
            })
            attempts.append({
                "inviteeUrn": f"urn:li:fsd_profile:{fsd_id}",
                **({"customMessage": note[:300]} if note else {}),
            })
        if member_id:
            attempts.append({
                "inviteeUrn": f"urn:li:member:{member_id}",
                **({"customMessage": note[:300]} if note else {}),
            })

        for payload in attempts:
            try:
                resp = await client.post(
                    f"{VOYAGER_BASE}/growth/normInvitations",
                    content=json.dumps(payload),
                    headers=headers,
                )
                if resp.status_code in (200, 201):
                    logger.info("Connection request sent to %s", slug)
                    return {"ok": True}
                last_err = Exception(
                    f"LinkedIn {resp.status_code}: {resp.text[:300]}"
                )
            except Exception as e:
                last_err = e

        raise ValueError(str(last_err))
