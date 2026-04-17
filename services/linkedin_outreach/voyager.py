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


def _extract_urn_id(urn: str) -> str:
    """Pull the ID fragment from any LinkedIn URN, e.g. 'urn:li:fsd_profile:ACoAA' → 'ACoAA'."""
    if urn and ":" in urn:
        fragment = urn.rsplit(":", 1)[-1]
        if len(fragment) > 4:  # skip short/garbage values
            return fragment
    return ""


async def _get_profile_id(
    client: httpx.AsyncClient,
    headers: dict,
    slug: str,
) -> str:
    """Resolve a public LinkedIn slug to its internal profile ID.

    Uses three strategies in order, stopping at the first success.
    """
    ref_headers = {**headers, "Referer": f"https://www.linkedin.com/in/{slug}/"}

    # ── Strategy 1: dash/profiles (newest, most stable as of 2024) ────────────
    try:
        resp = await client.get(
            f"{VOYAGER_BASE}/identity/dash/profiles",
            params={"q": "memberIdentity", "memberIdentity": slug,
                    "decorationId": "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93"},
            headers=ref_headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Dash format: data.identityDashProfilesByMemberIdentity.elements[]
            elements = (
                data.get("data", {})
                    .get("identityDashProfilesByMemberIdentity", {})
                    .get("elements", [])
            ) or data.get("elements", [])
            for el in elements:
                uid = _extract_urn_id(el.get("entityUrn", ""))
                if uid:
                    return uid
            # Also check included[]
            for entity in data.get("included", []):
                urn = entity.get("entityUrn", "")
                if any(t in urn for t in ("fsd_profile", "fs_profile", "fs_miniProfile")):
                    uid = _extract_urn_id(urn)
                    if uid:
                        return uid
    except Exception:
        pass

    # ── Strategy 2: basic identity/profiles (older endpoint, often still works) ─
    try:
        resp = await client.get(
            f"{VOYAGER_BASE}/identity/profiles/{slug}",
            headers=ref_headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Top-level entityUrn
            for key in ("entityUrn", "objectUrn"):
                uid = _extract_urn_id(data.get(key, "") or data.get("data", {}).get(key, ""))
                if uid:
                    return uid
            # included[]
            for entity in data.get("included", []):
                etype = entity.get("$type", "")
                if "profile" in etype.lower() or "miniProfile" in etype.lower():
                    uid = _extract_urn_id(entity.get("entityUrn", ""))
                    if uid:
                        return uid
    except Exception:
        pass

    # ── Strategy 3: networkinfo (light endpoint, returns entityUrn) ────────────
    try:
        resp = await client.get(
            f"{VOYAGER_BASE}/identity/profiles/{slug}/networkinfo",
            headers=ref_headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            uid = _extract_urn_id(
                data.get("entityUrn", "")
                or data.get("data", {}).get("entityUrn", "")
            )
            if uid:
                return uid
    except Exception:
        pass

    raise ValueError(
        f"Could not resolve LinkedIn profile '{slug}'. "
        "Check the URL is correct and the profile is publicly visible."
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

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(25.0)) as client:
        profile_id = await _get_profile_id(client, headers, slug)

        # Try both URN formats — LinkedIn changed the accepted format over time
        last_err: Exception | None = None
        for urn_prefix in ("urn:li:fsd_profile", "urn:li:fs_miniProfile"):
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
                    "recipients": [f"{urn_prefix}:{profile_id}"],
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
                    logger.info("Message sent to %s via %s", slug, urn_prefix)
                    return {"ok": True}
                last_err = Exception(f"LinkedIn returned {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                last_err = e

        raise ValueError(f"Could not send message: {last_err}")


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

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(25.0)) as client:
        profile_id = await _get_profile_id(client, headers, slug)

        # Try newer dash invitations endpoint first, fall back to normInvitations
        last_err: Exception | None = None
        attempts = [
            # Newer format
            (f"{VOYAGER_BASE}/growth/normInvitations", {
                "invitee": {
                    "com.linkedin.voyager.growth.invitation.InviteeProfile": {
                        "profileId": profile_id,
                    }
                },
                **({"customMessage": note[:300]} if note else {}),
            }),
            # Alternate format with trackingId
            (f"{VOYAGER_BASE}/growth/normInvitations", {
                "inviteeUrn": f"urn:li:fsd_profile:{profile_id}",
                **({"customMessage": note[:300]} if note else {}),
            }),
        ]
        for url, payload in attempts:
            try:
                resp = await client.post(
                    url, content=json.dumps(payload), headers=headers,
                )
                if resp.status_code in (200, 201):
                    logger.info("Connection request sent to %s", slug)
                    return {"ok": True}
                last_err = Exception(f"LinkedIn returned {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                last_err = e

        raise ValueError(f"Could not send connection request: {last_err}")
