-- Authoritative run <-> task association. Before this, the dashboard had to GUESS which task a run
-- worked: for a live run it inferred the task from whatever sat in a role's in-flight status, and for
-- a finished run it substring-matched task ids out of the free-text `summary`. Both are unreliable —
-- the summary is a project-wide `updated_at >= run start` diff, so it is blank on interrupt and
-- actor/concurrency-blind (a founder edit or a second parallel agent lands in the same window).
--
-- Instead, every task mutation made THROUGH the dais CLI while a run is active (run-agent.sh exports
-- DAIS_RUN_ID for the duration of an agent run) records a row here against the acting run. Founder
-- actions from a plain shell have no DAIS_RUN_ID, so they are correctly NOT attributed to any run.
-- One row per mutation (not per task) so the trail is complete: 'claim' (a build run picked the task
-- up: status -> doing), 'create' (task add), 'touch' (any other change). See link_run_task in the CLI.
--
-- Added as a migration (not the base schema) so existing dais.db files pick it up via `dais migrate`,
-- exactly like 0001. Runs that predate it have no rows here; the dashboard falls back to the summary
-- scan for those, so history still renders.
CREATE TABLE IF NOT EXISTS run_tasks (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id   INTEGER NOT NULL,
  task_id  TEXT    NOT NULL,
  verb     TEXT    NOT NULL DEFAULT 'touch',   -- claim | create | touch
  at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_run_tasks_task ON run_tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_run_tasks_run  ON run_tasks(run_id);
