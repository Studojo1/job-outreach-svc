-- LinkedIn outreach: pacing, note toggle, and optional pre-connect post-like
--
-- send_with_note: when FALSE, the daemon sends a no-note invite (LinkedIn's
--   data shows ~10pt higher acceptance without a note). Default FALSE so new
--   campaigns get the better acceptance rate by default — surfaced in UI with
--   a disclaimer if the user opts in.
-- weekly_invite_limit: per-campaign cap on invites sent in a rolling 7-day
--   window. Set to a random 92-97 at campaign launch so we stay safely below
--   LinkedIn's ~100/week soft cap without all campaigns hitting the same
--   round number (which makes the pacing look templated).
-- like_post_before_connect: optional. When TRUE, the daemon visits the lead's
--   recent activity, likes their latest post, then sends the invite. Skipped
--   silently if no post is found.

ALTER TABLE linkedin_campaigns
  ADD COLUMN IF NOT EXISTS send_with_note BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS weekly_invite_limit INTEGER NOT NULL DEFAULT 95,
  ADD COLUMN IF NOT EXISTS like_post_before_connect BOOLEAN NOT NULL DEFAULT FALSE;
