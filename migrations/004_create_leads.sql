-- 004_create_leads.sql

CREATE TABLE IF NOT EXISTS leads (
    id SERIAL PRIMARY KEY,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    apollo_id VARCHAR(255) UNIQUE,
    name VARCHAR(255),
    title VARCHAR(255),
    company VARCHAR(255),
    industry VARCHAR(255),
    location VARCHAR(255),
    linkedin_url TEXT,
    email VARCHAR(255),
    status VARCHAR(50) DEFAULT 'new',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
