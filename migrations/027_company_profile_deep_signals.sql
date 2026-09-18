-- Round-2 quality upgrade: deeper company signals.
-- Adds columns to company_profiles for multi-page scrape, Apollo
-- momentum signals, Apollo job postings, and the per-company
-- structured fact extractor output.

-- Multi-page scrape sections (WS1).
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS product_page_summary TEXT;
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS blog_summary TEXT;
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS careers_summary TEXT;

-- Deeper Apollo /organizations/enrich fields (WS2).
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS recent_news_summary TEXT;
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS latest_funding_round_date DATE;
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS latest_funding_amount BIGINT;
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS linkedin_url TEXT;

-- Apollo job postings (WS3): list of {title, snippet, posted_at}.
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS recent_job_postings JSONB;

-- Structured fact extractor output (WS4): {what_they_build, core_tech,
-- primary_market, stage_signal, recent_momentum, hiring_signal}.
ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS extracted_facts JSONB;
