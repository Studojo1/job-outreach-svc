-- 005_create_lead_scores.sql

CREATE TABLE IF NOT EXISTS lead_scores (
    id SERIAL PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    overall_score INTEGER NOT NULL,
    title_relevance INTEGER NOT NULL,
    department_relevance INTEGER NOT NULL,
    industry_relevance INTEGER NOT NULL,
    seniority_relevance INTEGER NOT NULL,
    location_relevance INTEGER NOT NULL,
    explanation TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
