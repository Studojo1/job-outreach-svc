-- Apollo credit-burn tracking: one row per IST calendar day.
-- `reveals` is incremented by the enrichment service on every successful
-- Apollo /people/match email reveal (= 1 credit). `alerted_at` is set by the
-- emailer-service once it has paged ops for that day, so we alert at most once.
CREATE TABLE IF NOT EXISTS apollo_usage_daily (
    day        date PRIMARY KEY,
    reveals    integer NOT NULL DEFAULT 0,
    alerted_at timestamptz
);
