"""Apollo API Integration Service.

Handles direct HTTP communication with Apollo's APIs, including
authentication, rate-limit throttling, and error handling.
"""

import json
import time
from typing import Dict, Any, List

from core.logger import get_logger
from services.shared.apollo_key_manager import apollo_post, apollo_keys

logger = get_logger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
APOLLO_MAX_TITLES = 150


def chunk_list(lst: List, size: int):
    """Yield successive chunks of `size` from `lst`."""
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def search_people(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a mixed_people/api_search query against Apollo.

    Raises:
        ValueError: If no valid Apollo API key is available.
        requests.HTTPError: If the API call fails after key rotation.
    """
    if not apollo_keys.has_valid_key():
        logger.error("Apollo API requested but no valid APOLLO_API_KEY configured")
        raise ValueError("No valid Apollo API key available")

    url = f"{APOLLO_BASE_URL}/mixed_people/api_search"

    logger.info(
        "[APOLLO_INPUT] page=%s, titles=%d, industries=%s, person_locations=%s, "
        "org_locations=%s, email_status=%s",
        payload.get("page", 1), len(payload.get("person_titles", [])),
        payload.get("organization_industries", []),
        payload.get("person_locations", []),
        payload.get("organization_locations", []),
        payload.get("contact_email_status", []),
    )
    logger.debug("[APOLLO_PAYLOAD] %s", json.dumps(payload, default=str)[:3000])

    time.sleep(0.2)

    response = apollo_post(url, json=payload.copy(), timeout=30)

    try:
        resp_json = response.json() if response.ok else {}
        people_count = len(resp_json.get("people", []))
        total_entries = resp_json.get("pagination", {}).get(
            "total_entries", resp_json.get("total_entries", 0)
        )
        logger.info(
            "[APOLLO_OUTPUT] status=%d, people_returned=%d, total_available=%d, page=%s",
            response.status_code, people_count, total_entries, payload.get("page", 1),
        )
    except Exception:
        logger.info("[APOLLO_OUTPUT] status=%d", response.status_code)

    if not response.ok:
        logger.error("Apollo API Error: HTTP %d - %s", response.status_code, response.text)
        response.raise_for_status()

    return response.json()


def search_people_count(payload: Dict[str, Any]) -> int:
    """Execute a count-only Apollo search (per_page=1).

    If person_titles exceeds Apollo's 150-title limit, the titles are
    automatically split into chunks and the total_entries from each
    chunk are summed to produce an aggregated count.
    """
    count_payload = payload.copy()
    count_payload["per_page"] = 1
    count_payload["page"] = 1

    titles = count_payload.get("person_titles", [])

    if len(titles) <= APOLLO_MAX_TITLES:
        result = search_people(count_payload)
        total = result.get("pagination", {}).get("total_entries", 0)
        if total == 0:
            total = result.get("total_entries", 0)
        logger.info("Apollo count query returned total_entries=%d", total)
        return total

    chunks = list(chunk_list(titles, APOLLO_MAX_TITLES))
    total_entries = 0

    for idx, title_chunk in enumerate(chunks, start=1):
        chunk_payload = count_payload.copy()
        chunk_payload["person_titles"] = title_chunk
        result = search_people(chunk_payload)
        chunk_total = result.get("pagination", {}).get("total_entries", 0)
        if chunk_total == 0:
            chunk_total = result.get("total_entries", 0)
        total_entries += chunk_total
        logger.info("[APOLLO CHUNK SEARCH] chunk %d/%d returned %d leads", idx, len(chunks), chunk_total)

    logger.info(
        "Apollo chunked count query aggregated total_entries=%d across %d chunks",
        total_entries, len(chunks),
    )
    return total_entries


def search_people_chunked(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute Apollo search with automatic title chunking.

    If person_titles exceeds 150, splits into chunks and merges
    the people arrays from each response. Returns a single combined
    response dict with aggregated people and total_entries.
    """
    titles = payload.get("person_titles", [])

    if len(titles) <= APOLLO_MAX_TITLES:
        return search_people(payload)

    chunks = list(chunk_list(titles, APOLLO_MAX_TITLES))
    all_people: List[Dict[str, Any]] = []
    total_entries = 0

    for idx, title_chunk in enumerate(chunks, start=1):
        chunk_payload = payload.copy()
        chunk_payload["person_titles"] = title_chunk
        result = search_people(chunk_payload)
        chunk_people = result.get("people", [])
        chunk_total = result.get("pagination", {}).get("total_entries", 0)
        if chunk_total == 0:
            chunk_total = result.get("total_entries", 0)
        all_people.extend(chunk_people)
        total_entries += chunk_total
        logger.info(
            "[APOLLO CHUNK SEARCH] chunk %d/%d returned %d leads (%d people)",
            idx, len(chunks), chunk_total, len(chunk_people),
        )

    logger.info(
        "Apollo chunked search aggregated %d total_entries, %d people across %d chunks",
        total_entries, len(all_people), len(chunks),
    )
    return {"people": all_people, "total_entries": total_entries}
