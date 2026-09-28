-- 050_data_health_alerts.sql
-- One row per data-health check per IST day on which ops were paged.
--
-- emailer-service checks invariants hourly (users without a login, resumes
-- without a profile, duplicate unused candidates, quizzes completed without
-- roles; see emailer internal/store/data_health.go) and pages ops at most once
-- per check per day. It remembered that in memory, so a restart or a deploy
-- during an ongoing problem sent the same alert again. The emailer claims the
-- (check, day) row before sending; the primary key makes the claim atomic.
-- Safe to re-run.
CREATE TABLE IF NOT EXISTS data_health_alerts (
  check_name  text        NOT NULL,
  day         date        NOT NULL,
  count       integer     NOT NULL,
  alerted_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (check_name, day)
);
