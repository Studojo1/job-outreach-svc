"""Apollo /mixed_companies/search wrapper — disambiguates a company name to a canonical domain.

Why this exists:
  Apollo's free people search returns only `organization.name` (a string like "Swish")
  with no domain. For ambiguous short names (Comet, Swish, Apex, Pulse, ...) multiple
  real companies share the name. Previously we let the LLM web search guess the
  domain from name alone, which biased to the largest / most-indexed company and
  attributed Indian-startup contacts to unrelated US/EU brands.

  Apollo's /mixed_companies/search lets us pass `q_organization_name` along with
  `organization_locations` — the location filter reliably surfaces the right org as
  the top result (verified live for "Swish" India → justswish.in, "Comet" India →
  wearcomet.com).

Cost: FREE. Same rate-limit class as people search (200/min, 6k/hr, 50k/day). No
credits deducted. Verified via response headers.
"""

import logging
from typing import List, Optional, Dict, Any

from services.shared.apollo_key_manager import apollo_post, apollo_keys

logger = logging.getLogger(__name__)

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"


def resolve_canonical_domain(
    name: str,
    location_hint: Optional[List[str]] = None,
    timeout: int = 15,
) -> Optional[Dict[str, Any]]:
    """Resolve a company name to its canonical domain via Apollo company search.

    Args:
        name: Company name from Apollo people search (e.g. "Swish").
        location_hint: Optional list of locations to bias the search
            (e.g. ["India"] or ["Bangalore"]). Falls through to no-filter
            search if location-filtered returns zero results.
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with at least `primary_domain`, optionally `name`, `linkedin_url`,
        `founded_year`, `linkedin_uid`, `apollo_org_id`, and headcount growth.
        Returns None if Apollo finds no matching org or the call fails.
    """
    if not name or not name.strip():
        return None
    if not apollo_keys.has_valid_key():
        logger.warning("[COMPANY_RESOLVE] No valid Apollo key; skipping")
        return None

    clean_name = name.strip()
    candidates: List[Optional[List[str]]] = []
    if location_hint:
        candidates.append([loc for loc in location_hint if loc])
    candidates.append(None)  # always retry with no location filter if needed

    for locs in candidates:
        payload: Dict[str, Any] = {
            "q_organization_name": clean_name,
            "per_page": 3,
            "page": 1,
        }
        if locs:
            payload["organization_locations"] = locs

        try:
            resp = apollo_post(APOLLO_SEARCH_URL, json=payload, timeout=timeout)
        except Exception as e:
            logger.warning("[COMPANY_RESOLVE] network error for %r: %s", clean_name, e)
            return None
        if not resp.ok:
            logger.debug("[COMPANY_RESOLVE] HTTP %d for %r (locs=%s)", resp.status_code, clean_name, locs)
            continue

        try:
            data = resp.json() or {}
        except ValueError:
            continue

        orgs = data.get("organizations") or data.get("accounts") or []
        if not orgs:
            continue

        top = orgs[0]
        primary_domain = _clean_domain(top.get("primary_domain") or top.get("website_url"))
        if not primary_domain:
            continue

        resolved = {
            "primary_domain": primary_domain,
            "name": top.get("name") or clean_name,
            "linkedin_url": top.get("linkedin_url"),
            "linkedin_uid": top.get("linkedin_uid"),
            "founded_year": top.get("founded_year"),
            "apollo_org_id": top.get("id"),
            "headcount_growth_6mo": top.get("organization_headcount_six_month_growth"),
            "headcount_growth_12mo": top.get("organization_headcount_twelve_month_growth"),
            "headcount_growth_24mo": top.get("organization_headcount_twenty_four_month_growth"),
            "_location_filtered": bool(locs),
        }
        logger.info(
            "[COMPANY_RESOLVE] %r → %s (locs=%s, top of %d results)",
            clean_name, primary_domain, locs, len(orgs),
        )
        return resolved

    logger.info("[COMPANY_RESOLVE] no Apollo org match for %r (tried %d filter set(s))", clean_name, len(candidates))
    return None


def _clean_domain(raw: Optional[str]) -> Optional[str]:
    """Normalise a URL or domain string to a lowercase bare host."""
    if not raw or not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v.startswith(("http://", "https://")):
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("www."):
        v = v[4:]
    return v or None
