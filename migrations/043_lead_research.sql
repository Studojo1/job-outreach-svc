-- 043: per-person deep research (context.dev) + depth-guard verdict.
--
-- Keyed on PERSON, not on lead. `leads` holds one row per (candidate, person):
-- migration 009 explicitly dropped the UNIQUE constraint on leads.apollo_id with
-- the comment "should allow duplicates across candidates". So two students who
-- both target the same hiring manager produce two `leads` rows for one human.
-- Keying research on lead_id would research — and bill — that person twice.
--
-- This mirrors company_profiles, which is cached globally on `domain` "so the same
-- company doesn't get re-enriched per user". Same reasoning, same shape.
--
-- person_key = COALESCE(leads.apollo_id, leads.linkedin_url). Never null:
--   * Apollo-sourced leads set both (lead_collector_service.py:600)
--   * LinkedIn-sourced leads set linkedin_url and skip rows without one
--     (routes_discovery.py:877)
--
-- Research is fetched just-in-time by the campaign worker (Phase 0), inside the
-- same lookahead window as Apollo enrichment, so leads that bounce, no-match, or
-- never send are never researched and never billed.
--
-- The depth guard's verdict is stored, not just applied: `survives_swap = true`
-- means the synthesis line would read true for a different person in the same
-- role, so it was rejected and the email dropped to the bare ask. Keeping the
-- rejected line makes the kill rate auditable.

CREATE TABLE IF NOT EXISTS lead_research (
    id              SERIAL PRIMARY KEY,

    -- Stable person identity, shared across every candidate targeting them.
    -- UNIQUE is load-bearing: the app inserts this row BEFORE spending any
    -- credits, so the constraint is the cross-replica mutex that stops 2-6
    -- concurrently-running pods from double-spending on the same person.
    person_key      TEXT NOT NULL UNIQUE,

    -- Fetch lifecycle: pending -> fetched | no_signal | failed
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    credits_spent   INTEGER NOT NULL DEFAULT 0,   -- 0..3; a 2-credit lead is a good lead
    fetched_urls    JSONB,                        -- dedupe set; Call 3 must never refetch Call 2
    error_message   TEXT,

    -- The four layers, in priority order. Any may be null.
    quote               TEXT,   -- their own words (a post / an X reply). NOT a comment: unreachable anonymously.
    quote_source_url    TEXT,
    derived_operational TEXT,   -- what the title required, from a career table. Available for nearly every lead.
    behavioural         TEXT,   -- the unguarded signal. Often why a stranger replies.
    live_move           TEXT,   -- texture only. Never load-bearing.

    -- Depth guard
    synthesis_line  TEXT,           -- the proposed line, both halves (kept even when rejected)
    -- The recipient's half of the line, with the sender's clause stripped. The email
    -- writer renders this as a fact ABOUT the recipient, so it must never contain
    -- anything the sender did, or the email credits them with the sender's work.
    recipient_clause TEXT,
    -- The "why now" clause: a company fact (a store rollout, a launch) that makes the
    -- recipient's problem bigger. Rendered separately so it is never attributed to the
    -- person. NULL when no signal earned its place.
    bridge_clause   TEXT,
    survives_swap   BOOLEAN,        -- TRUE = too shallow = rejected = bare ask
    guard_layer     VARCHAR(32),    -- which layer fed the line
    guard_reason    TEXT,

    -- Staleness. A person's research goes out of date: they change jobs, they post.
    --
    -- TIMESTAMP WITHOUT TIME ZONE, not TIMESTAMPTZ. Every other table in this
    -- schema (emails_sent, company_profiles, leads) is naive, and the app writes
    -- datetime.utcnow(). A timestamptz column here would make Postgres reinterpret
    -- those naive UTC values in the server's local timezone, silently skewing every
    -- staleness comparison by the server's UTC offset.
    fetched_at      TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at      TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
);

-- Phase 0 scans for people still needing research.
CREATE INDEX IF NOT EXISTS idx_lead_research_status ON lead_research(status);

-- Re-research anything older than the staleness window.
CREATE INDEX IF NOT EXISTS idx_lead_research_fetched_at ON lead_research(fetched_at);

-- A shipped line is one that was fetched AND failed the swap test.
CREATE INDEX IF NOT EXISTS idx_lead_research_shippable
    ON lead_research(person_key) WHERE survives_swap IS FALSE;
