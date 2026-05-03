"""Company Intelligence — globally cached enrichment for the lead set.

Two data sources:
- Apollo /organizations/enrich (free under our plan, no people-credit cost)
- Light homepage + /about scrape (httpx + BeautifulSoup) for top-K only

All results are cached in the `company_profiles` table keyed by domain so the
same company doesn't get re-enriched per user.
"""
