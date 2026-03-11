"""Apollo API Integration Service.

Handles direct HTTP communication with Apollo's APIs, including
authentication, rate-limit throttling, and error handling.
"""

import time
import requests
from typing import Dict, Any, List

from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
APOLLO_MAX_TITLES = 150


def chunk_list(lst: List, size: int):
    """Yield successive chunks of `size` from `lst`."""
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def search_people(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a mixed_people/api_search query against Apollo.

    Args:
        payload: JSON-serializable dict built by the query builder.

    Returns:
        The JSON response from Apollo.

    Raises:
        requests.HTTPError: If the API call fails.
        ValueError: If the APOLLO_API_KEY is missing.
    """
    if not settings.APOLLO_API_KEY or settings.APOLLO_API_KEY == "your_apollo_key_here":
        logger.error("Apollo API requested but APOLLO_API_KEY is not properly set")
        raise ValueError("APOLLO_API_KEY environment variable is not set")

    url = f"{APOLLO_BASE_URL}/mixed_people/api_search"
    
    request_data = payload.copy()

    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": settings.APOLLO_API_KEY
    }

    print("========== APOLLO REQUEST ==========")
    print(payload)

    logger.info("Executing Apollo People Search via POST %s (page=%s)", url, payload.get("page", 1))

    # Throttle request to protect against rate limits (as dictated by design docs)
    time.sleep(0.2)

    response = requests.post(url, json=request_data, headers=headers)
    
    print("========== APOLLO RESPONSE STATUS ==========")
    print(response.status_code)

    print("========== APOLLO RESPONSE BODY ==========")
    print(response.text)

    try:
        print("========== APOLLO RESPONSE JSON ==========")
        print(response.json())
    except:
        print("Apollo response is not JSON")
        
    if not response.ok:
        logger.error(
            "Apollo API Error: HTTP %d - %s",
            response.status_code, response.text
        )
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
        # Single request — titles fit within limit
        result = search_people(count_payload)
        total = result.get("pagination", {}).get("total_entries", 0)
        if total == 0:
            total = result.get("total_entries", 0)
        logger.info("Apollo count query returned total_entries=%d", total)
        return total

    # ── Chunked search ───────────────────────────────────────────────────
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
        print(f"[APOLLO CHUNK SEARCH] chunk {idx}/{len(chunks)} returned {chunk_total} leads")
        logger.info("[APOLLO CHUNK SEARCH] chunk %d/%d returned %d leads", idx, len(chunks), chunk_total)

    logger.info("Apollo chunked count query aggregated total_entries=%d across %d chunks", total_entries, len(chunks))
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

    # ── Chunked search ───────────────────────────────────────────────────
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
        print(f"[APOLLO CHUNK SEARCH] chunk {idx}/{len(chunks)} returned {chunk_total} leads ({len(chunk_people)} people)")
        logger.info("[APOLLO CHUNK SEARCH] chunk %d/%d returned %d leads (%d people)", idx, len(chunks), chunk_total, len(chunk_people))

    logger.info("Apollo chunked search aggregated %d total_entries, %d people across %d chunks", total_entries, len(all_people), len(chunks))

    return {
        "people": all_people,
        "total_entries": total_entries,
    }
