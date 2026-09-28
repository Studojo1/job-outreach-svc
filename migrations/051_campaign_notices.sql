-- 051_campaign_notices.sql
-- Post-payment audit P09/P38/P42: customers are told about their campaign.
--
-- services/campaign_notices.py emails a customer when their Gmail needs
-- reconnecting, when a running campaign stalls, when a paused campaign has
-- sat with unsent work for a week, and when a campaign finishes (with the
-- real delivered / skipped split). One row per notice sent; the unique key
-- is the dedupe, so a restart or a second replica cannot double-send.
-- `occurrence` distinguishes repeats of the same kind (for example the
-- paused_at of a particular pause), so a campaign paused twice is told twice.
--
-- Apply BEFORE deploying the code that maps it (CampaignNotice).

CREATE TABLE IF NOT EXISTS campaign_notices (
    id          BIGSERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    kind        VARCHAR(30) NOT NULL,
    occurrence  VARCHAR(40) NOT NULL DEFAULT '',
    created_at  TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT campaign_notices_once UNIQUE (campaign_id, kind, occurrence)
);
CREATE INDEX IF NOT EXISTS ix_campaign_notices_user ON campaign_notices (user_id, created_at);
