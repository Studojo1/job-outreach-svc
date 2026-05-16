-- LKOT student-quiz integration
--
-- candidate_id: links an LKOT campaign to the student's candidate profile
-- (resume_profile + quiz output drive targeting + per-lead personalisation).
--
-- match_reason: one-line "why this lead is a match" string per connection request.
-- Generated when leads are saved (alongside the personalised connection_note)
-- so the student sees why each person is worth connecting with.

ALTER TABLE linkedin_campaigns
  ADD COLUMN IF NOT EXISTS candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL;

ALTER TABLE linkedin_connection_requests
  ADD COLUMN IF NOT EXISTS match_reason TEXT;
