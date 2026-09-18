"""Company Intelligence — globally cached enrichment for the lead set.

Pipeline (all Apollo calls below are FREE — no per-credit cost):
- Apollo /mixed_companies/search → resolve canonical domain from company name
  + location hint (`apollo_company_resolver.py`). Disambiguates ambiguous
  short names like "Comet" or "Swish" to the right org.
- LLM web research via Azure Responses API + Bing (`llm_company_research.py`)
  → extracts what_they_build, core_tech, primary_market, hiring_signal, etc.
- Light homepage + /about scrape (httpx + BeautifulSoup) for top-K only.

All results are cached in the `company_profiles` table keyed by domain so the
same company doesn't get re-enriched per user.
"""
