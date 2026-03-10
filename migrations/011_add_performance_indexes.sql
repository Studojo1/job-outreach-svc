-- Migration 011: Performance indexes on foreign keys
CREATE INDEX IF NOT EXISTS idx_leads_candidate_id ON leads(candidate_id);
CREATE INDEX IF NOT EXISTS idx_lead_scores_lead_id ON lead_scores(lead_id);
CREATE INDEX IF NOT EXISTS idx_emails_sent_campaign_id ON emails_sent(campaign_id);
CREATE INDEX IF NOT EXISTS idx_candidates_user_id ON candidates(user_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_candidate_id ON campaigns(candidate_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_email_account_id ON campaigns(email_account_id);
CREATE INDEX IF NOT EXISTS idx_email_accounts_user_id ON email_accounts(user_id);
