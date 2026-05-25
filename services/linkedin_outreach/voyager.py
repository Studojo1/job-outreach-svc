"""LinkedIn Voyager API client — authenticated people search and messaging using li_at session."""

import asyncio
import json
import logging
import random
import re
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# LinkedIn Voyager API base
VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

# GraphQL query ID for people search (replaced deprecated /search/blended)
_SEARCH_QUERY_ID = "voyagerSearchDashClusters.b0928897b71bd00a5a7291755dcd64f0"

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


def _parse_graphql_search_results(data: dict) -> list[dict]:
    """Parse LinkedIn GraphQL search response into a flat list of people dicts.

    LinkedIn's normalized response stores entityResult objects in the top-level
    'included' array and references them via '*entityResult' URN keys in items.
    Both inline ('entityResult') and reference ('*entityResult') formats are handled.
    """
    # Build URN -> object lookup from the normalized included array
    by_urn: dict = {obj["entityUrn"]: obj for obj in data.get("included", []) if obj.get("entityUrn")}

    people = []
    # Navigate: data.data.searchDashClustersByAll (double-nested 'data' in GraphQL response)
    outer = data.get("data") or {}
    clusters_root = outer.get("searchDashClustersByAll") or (outer.get("data") or {}).get("searchDashClustersByAll") or {}
    for cluster in clusters_root.get("elements", []):
        for item in cluster.get("items", []):
            item_data = item.get("item") or {}
            # Inline entity (old format)
            entity = item_data.get("entityResult") or {}
            # Reference entity (new normalized format)
            if not entity:
                ref = item_data.get("*entityResult")
                if ref:
                    entity = by_urn.get(ref, {})
            if not entity:
                continue
            name = ((entity.get("title") or {}).get("text") or "").strip()
            if not name:
                continue
            nav_url = entity.get("navigationUrl") or ""
            if "/in/" not in nav_url:
                continue
            profile_url = re.sub(r"\?.*", "", nav_url).rstrip("/") + "/"
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


async def search_people(
    li_at: str,
    jsessionid: str,
    keywords: str,
    location: Optional[str] = None,
    count: int = 10,
    proxy_url: Optional[str] = None,
) -> list[dict]:
    """Search LinkedIn for people matching keywords.

    Returns a list of dicts with: name, headline, company, profile_url, profile_image_url.
    Raises ValueError on auth failure or rate limit.
    """
    await asyncio.sleep(random.uniform(1.5, 3.0))

    kw = f"{keywords} {location}".strip() if location else keywords
    # Encode only the keyword value; structural chars (, : ( )) must remain literal
    encoded_kw = quote(kw, safe="")
    variables = (
        f"(start:0,count:{count},origin:GLOBAL_SEARCH_HEADER,"
        f"query:(keywords:{encoded_kw},flagshipSearchIntent:SEARCH_SRP,"
        f"queryParameters:List((key:resultType,value:List(PEOPLE))),"
        f"includeFiltersInResponse:false))"
    )
    url = (
        f"{VOYAGER_BASE}/graphql"
        f"?includeWebMetadata=true"
        f"&variables={variables}"
        f"&queryId={_SEARCH_QUERY_ID}"
    )

    headers = _build_headers(li_at, jsessionid)
    proxy_map = {"http://": proxy_url, "https://": proxy_url} if proxy_url else None

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
        proxies=proxy_map,
    ) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code in (401, 403):
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

    people = _parse_graphql_search_results(data)
    logger.info("LinkedIn search '%s' returned %d people", keywords, len(people))
    return people


async def resolve_linkedin_url(
    first_name: str,
    company: str,
    title: str,
    li_at: str,
    jsessionid: str,
    proxy_url: Optional[str] = None,
) -> Optional[str]:
    """Given Apollo-free data (first name + company + title), find the LinkedIn profile URL.

    Searches Voyager for "{first_name} {company}", then scores results by
    name + title similarity to pick the best match.
    Returns the profile URL string or None if no confident match found.
    """
    keywords = f"{first_name} {company}".strip()
    try:
        results = await search_people(li_at, jsessionid, keywords, count=5, proxy_url=proxy_url)
    except ValueError as e:
        # search_people raises ValueError on 401/403 ("LinkedIn session
        # expired") and 429 ("rate limited"). Both signal the daemon should
        # back off — surface as LinkedInAuthError so the caller's
        # auth-failure threshold ticks instead of treating it as a missing
        # profile and retrying immediately.
        msg = str(e)
        if "session expired" in msg.lower() or "rate limited" in msg.lower():
            from services.linkedin_outreach.automation_service import LinkedInAuthError
            raise LinkedInAuthError(msg) from e
        logger.warning("Voyager resolve failed for '%s' at '%s': %s", first_name, company, e)
        return None
    except Exception as e:
        logger.warning("Voyager resolve failed for '%s' at '%s': %s", first_name, company, e)
        return None

    if not results:
        return None

    first_lower = first_name.lower()
    title_words = set(title.lower().split()) - {"of", "and", "the", "at", "in", "for", "a"}
    company_lower = company.lower()

    best_url: Optional[str] = None
    best_score = -1

    for r in results:
        result_name = (r.get("name") or "").lower()
        result_headline = (r.get("headline") or "").lower()
        result_company = (r.get("company") or "").lower()

        # First name must match — skip entirely if not
        if not result_name.startswith(first_lower) and first_lower not in result_name:
            continue

        score = 3 if result_name.startswith(first_lower) else 1

        # Title / headline word overlap
        headline_words = set(result_headline.split())
        score += len(title_words & headline_words)

        # Company name overlap
        if company_lower and (company_lower in result_company or result_company in company_lower):
            score += 2

        if score > best_score:
            best_score = score
            best_url = r.get("profile_url")

    if best_url:
        logger.info(
            "Resolved '%s' at '%s' → %s (score=%d)", first_name, company, best_url, best_score
        )
    else:
        logger.debug("No match for '%s' at '%s'", first_name, company)

    return best_url


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
