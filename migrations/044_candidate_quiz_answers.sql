-- 044_candidate_quiz_answers.sql
-- Give the quiz somewhere to put an answer the moment it is given, instead of
-- holding all of them in the browser until the last turn.
--
-- Today POST /candidate/{id}/chat/stream is a pure replay: the client posts its
-- whole chat history, the endpoint rebuilds the answers dict from scratch on
-- every turn, serves the next question, and writes nothing. The only INSERT
-- happens in the quiz-complete branch. Three consequences, all of them live:
--
--   * A user who abandons at Q5 leaves no trace. 1,559 abandoned quizzes stored
--     zero answers, so per-question drop-off cannot be measured at all.
--   * A refresh, a back gesture or a mobile tab eviction destroys the quiz,
--     because the browser was the only copy of the state.
--   * Answers are keyed by array position during replay, so a retry that leaves
--     a duplicate user message in the history shifts every later answer onto
--     the wrong question key.
--
-- `quiz_answers` is the server-side copy. It is written before the SSE response
-- on every turn, upserted by question key, so the row converges on the same
-- dict the completion branch would have built. NULL on every existing row and
-- the replay path still works unchanged, so quizzes already in flight are
-- unaffected and nothing needs backfilling.
--
-- Keyed by question key, never by position — that is the whole point, and it is
-- what makes the off-by-one on retry fixable. A writer that stores a list here
-- reintroduces the bug this column exists to remove.

ALTER TABLE candidates ADD COLUMN IF NOT EXISTS quiz_answers JSONB;

-- Stamped on the first write and left alone afterwards, so "when did this user
-- actually start answering" is readable from the candidate row itself. The
-- existing outreach_orders.quiz_started_at is structurally unwritable (the
-- frontend serves Q1 locally and never sends the __start__ bootstrap the guard
-- requires), which is why it is set on 1 row out of 4,791.
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS quiz_answers_updated_at TIMESTAMP;

COMMENT ON COLUMN candidates.quiz_answers IS
    'Server-side copy of the quiz answers, keyed by question key, upserted on '
    'every turn of /candidate/{id}/chat/stream. NULL means the quiz predates '
    'this column or has not been started. Never a list: position-keyed answers '
    'are the bug this column removes.';

COMMENT ON COLUMN candidates.quiz_answers_updated_at IS
    'Last time quiz_answers was written. Set on the first answer, refreshed on '
    'each subsequent one, so an abandoned quiz is visible as a row whose '
    'answers stopped short.';
