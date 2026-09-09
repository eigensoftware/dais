-- The transition log (plan 4.2). run_tasks records what an AGENT RUN touched; the founder's own
-- fires (approve, request_changes, greenlight, …) were recorded nowhere, so "how often do I
-- approve this gate without changes?" — the yolo calibration question — had no data. Every
-- fire() now appends one row here, whoever fired it. `dais retro` reads it (gate decisions and
-- their wait, QA pass/fail, bounces, throughput); the web charts will. Rows start at migration.
CREATE TABLE IF NOT EXISTS task_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  project    TEXT,
  verb       TEXT NOT NULL,
  from_state TEXT,
  to_state   TEXT,
  actor      TEXT,
  at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_project_at ON task_events(project, at);
