-- Migration: Create outreach_orders table for order tracking
-- Purpose: Track full lifecycle of outreach runs so users can resume via My Orders

CREATE TABLE IF NOT EXISTS outreach_orders (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    email_account_id INTEGER REFERENCES email_accounts(id) ON DELETE SET NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'created',
    leads_collected INTEGER NOT NULL DEFAULT 0,
    leads_target INTEGER NOT NULL DEFAULT 500,
    action_log JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_outreach_orders_user_id ON outreach_orders(user_id);
CREATE INDEX idx_outreach_orders_status ON outreach_orders(status);

COMMENT ON TABLE outreach_orders IS 'Tracks the full lifecycle of an outreach run. Users can resume from My Orders.';
COMMENT ON COLUMN outreach_orders.status IS 'State machine: created → leads_generating → leads_ready → campaign_setup → email_connected → campaign_running → completed';
