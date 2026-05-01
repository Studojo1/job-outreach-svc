-- 025_funnel_stage_timestamps.sql
--
-- Add per-stage timestamps to outreach_orders so we can see the *journey*
-- of every user, not just their current status. The status field continues
-- to drive the state machine; these timestamps are append-only history.
--
-- Also adds started_at / completed_at to campaigns and an outreach_order_id
-- FK on payment_orders so the dashboard can stitch payments back to orders.

BEGIN;

-- ── outreach_orders: 12 stage timestamps ────────────────────────────────────
ALTER TABLE outreach_orders
  ADD COLUMN IF NOT EXISTS resume_uploaded_at        TIMESTAMP,
  ADD COLUMN IF NOT EXISTS quiz_started_at           TIMESTAMP,
  ADD COLUMN IF NOT EXISTS quiz_completed_at         TIMESTAMP,
  ADD COLUMN IF NOT EXISTS leads_generated_at        TIMESTAMP,
  ADD COLUMN IF NOT EXISTS payment_page_reached_at   TIMESTAMP,
  ADD COLUMN IF NOT EXISTS payment_made_at           TIMESTAMP,
  ADD COLUMN IF NOT EXISTS gmail_connected_at        TIMESTAMP,
  ADD COLUMN IF NOT EXISTS email_style_selected_at   TIMESTAMP,
  ADD COLUMN IF NOT EXISTS campaign_setup_at         TIMESTAMP,
  ADD COLUMN IF NOT EXISTS campaign_launched_at      TIMESTAMP,
  ADD COLUMN IF NOT EXISTS campaign_paused_at        TIMESTAMP,
  ADD COLUMN IF NOT EXISTS campaign_completed_at     TIMESTAMP;

-- ── campaigns: started_at / completed_at ────────────────────────────────────
ALTER TABLE campaigns
  ADD COLUMN IF NOT EXISTS started_at   TIMESTAMP,
  ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;

-- ── payment_orders: link to outreach_orders ─────────────────────────────────
ALTER TABLE payment_orders
  ADD COLUMN IF NOT EXISTS outreach_order_id INTEGER
    REFERENCES outreach_orders(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_payment_orders_outreach_order_id
  ON payment_orders(outreach_order_id);

-- ── Indexes for funnel aggregation queries ──────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_outreach_orders_resume_uploaded_at
  ON outreach_orders(resume_uploaded_at);
CREATE INDEX IF NOT EXISTS idx_outreach_orders_quiz_completed_at
  ON outreach_orders(quiz_completed_at);
CREATE INDEX IF NOT EXISTS idx_outreach_orders_payment_made_at
  ON outreach_orders(payment_made_at);

COMMIT;
