-- LinkedIn integration: add plan_type + LinkedIn columns to outreach_orders,
-- and plan_id to payment_orders so the new 9-tier plan IDs are stored.

ALTER TABLE outreach_orders
  ADD COLUMN IF NOT EXISTS plan_type TEXT NOT NULL DEFAULT 'email',
  ADD COLUMN IF NOT EXISTS linkedin_campaign_id INTEGER REFERENCES linkedin_campaigns(id),
  ADD COLUMN IF NOT EXISTS linkedin_connected_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS linkedin_credits_reserved INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS linkedin_credits_used INTEGER NOT NULL DEFAULT 0;

ALTER TABLE payment_orders
  ADD COLUMN IF NOT EXISTS plan_id TEXT;
