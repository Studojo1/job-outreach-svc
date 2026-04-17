"""LinkedIn Voyager API client — authenticated people search and messaging using li_at session."""

import asyncio
import json
import logging
import random
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


async def _get_mini_profile_urn(
    client: httpx.AsyncClient,
    headers: dict,
    slug: str,
) -> str:
    """Return the profile ID token for a public slug.

    Tries multiple Voyager endpoints in order and returns the first usable URN fragment.
    """
    profile_headers = {**headers, "Referer": f"https://www.linkedin.com/in/{slug}/"}

    # Strategy 1: basic profile endpoint (more reliable, lighter payload)
    try:
        resp = await client.get(
            f"{VOYAGER_BASE}/identity/profiles/{slug}",
            headers=profile_headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Look in included[] first
            for entity in data.get("included", []):
                etype = entity.get("$type", "")
                if "miniProfile" in etype.lower() or "MiniProfile" in etype:
                    urn = entity.get("entityUrn", "")
                    if urn and ":" in urn:
                        return urn.split(":")[-1]
            # Top-level entityUrn
            urn = data.get("entityUrn", "") or data.get("data", {}).get("entityUrn", "")
            if urn and ":" in urn:
                return urn.split(":")[-1]
    except Exception:
        pass

    # Strategy 2: profileView endpoint
    try:
        resp = await client.get(
            f"{VOYAGER_BASE}/identity/profiles/{slug}/profileView",
            headers=profile_headers,
        )
        _auth_check(resp)
        data = resp.json()
        for entity in data.get("included", []):
            etype = entity.get("$type", "")
            if "miniProfile" in etype.lower() or "Profile" in etype:
                urn = entity.get("entityUrn", "")
                if urn and ":" in urn:
                    candidate = urn.split(":")[-1]
                    # fs_miniProfile URNs start with "ACoAA" or similar base64-ish string
                    if len(candidate) > 5:
                        return candidate
        # Fallback to data.entityUrn
        for key in ("entityUrn", "objectUrn"):
            urn = data.get("data", {}).get(key, "")
            if urn and ":" in urn:
                return urn.split(":")[-1]
    except Exception as e:
        raise ValueError(f"Could not resolve profile '{slug}': {e}") from e

    raise ValueError(f"Could not find member URN for profile: {slug}")


# ── Message sending ───────────────────────────────────────────────────────────

async def send_linkedin_message(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    content: str,
) -> dict:
    """Send a direct message to a LinkedIn member via Voyager API.

    Returns {"ok": True} on success or raises ValueError with a user-facing message.
    """
    slug = _extract_slug(profile_url)
    headers = _build_headers(li_at, jsessionid)
    headers["Content-Type"] = "application/json"

    await asyncio.sleep(random.uniform(1.0, 2.0))

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(25.0)) as client:
        mini_urn = await _get_mini_profile_urn(client, headers, slug)

        payload = {
            "keyVersion": "LEGACY_INBOX",
            "conversationCreate": {
                "eventCreate": {
                    "value": {
                        "com.linkedin.voyager.messaging.create.MessageCreate": {
                            "attributedBody": {
                                "text": content,
                                "attributes": [],
                            },
                            "attachments": [],
                        }
                    }
                },
                "recipients": [f"urn:li:fs_miniProfile:{mini_urn}"],
                "subtype": "MEMBER_TO_MEMBER",
            },
        }

        resp = await client.post(
            f"{VOYAGER_BASE}/messaging/conversations",
            content=json.dumps(payload),
            headers=headers,
        )

    _auth_check(resp)
    logger.info("Message sent to %s", slug)
    return {"ok": True}


# ── Connection request ────────────────────────────────────────────────────────

async def send_linkedin_connection(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    note: str = "",
) -> dict:
    """Send a LinkedIn connection request, optionally with a personalised note.

    Returns {"ok": True} on success or raises ValueError with a user-facing message.
    """
    slug = _extract_slug(profile_url)
    headers = _build_headers(li_at, jsessionid)
    headers["Content-Type"] = "application/json"

    await asyncio.sleep(random.uniform(1.0, 2.0))

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(25.0)) as client:
        mini_urn = await _get_mini_profile_urn(client, headers, slug)

        payload: dict = {
            "invitee": {
                "com.linkedin.voyager.growth.invitation.InviteeProfile": {
                    "profileId": mini_urn,
                }
            },
        }
        if note:
            payload["customMessage"] = note[:300]

        resp = await client.post(
            f"{VOYAGER_BASE}/growth/normInvitations",
            content=json.dumps(payload),
            headers=headers,
        )

    _auth_check(resp)
    logger.info("Connection request sent to %s", slug)
    return {"ok": True}
