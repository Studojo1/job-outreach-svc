-- Migration: Add assigned_style column to emails_sent table
-- Purpose: Support email style selection and auto-assignment per lead

ALTER TABLE emails_sent
ADD COLUMN assigned_style VARCHAR(50) DEFAULT NULL;

-- Create index for querying by style
CREATE INDEX idx_emails_sent_assigned_style ON emails_sent(assigned_style);

-- Add comment
COMMENT ON COLUMN emails_sent.assigned_style IS 'Email writing style assigned to this email: warm_intro, value_prop, company_curiosity, peer_to_peer, direct_ask';
