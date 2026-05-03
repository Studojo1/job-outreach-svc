-- Company-level enrichment cache: shared across all users so a company gets
-- enriched (Apollo + scrape) once globally and reused.
CREATE TABLE IF NOT EXISTS company_profiles (
    id SERIAL PRIMARY KEY,
    domain TEXT UNIQUE NOT NULL,
    name TEXT,
    apollo_org_id TEXT,
    short_description TEXT,
    industries JSONB,
    keywords JSONB,
    technologies JSONB,
    employee_count INTEGER,
    founded_year INTEGER,
    headquarters_city TEXT,
    website_summary TEXT,
    scrape_meta_title TEXT,
    scrape_meta_description TEXT,
    scrape_hero_text TEXT,
    last_apollo_enriched_at TIMESTAMP,
    last_scraped_at TIMESTAMP,
    scrape_failed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_company_profiles_domain ON company_profiles(domain);
CREATE INDEX IF NOT EXISTS idx_company_profiles_apollo_org_id ON company_profiles(apollo_org_id);

-- Per-lead structured justification produced by the LLM justifier
-- (top-K leads only; bottom leads keep the existing `explanation` text).
ALTER TABLE lead_scores ADD COLUMN IF NOT EXISTS justification_json JSONB;

-- Company domain captured from Apollo people search organization payload.
-- Used as the join key against company_profiles for enrichment + LLM context.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_domain TEXT;
CREATE INDEX IF NOT EXISTS idx_leads_company_domain ON leads(company_domain);
