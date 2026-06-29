-- dais.db — the coordination spine for the agent company.
-- One row per unit of work; status transitions ARE the handoffs.

CREATE TABLE IF NOT EXISTS tasks (
  id         TEXT PRIMARY KEY,            -- e.g. win-2
  project    TEXT NOT NULL,               -- project name (config-driven; e.g. 'my-project')
  title      TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'backlog',
             -- see the canonical status set in the dais CLI (STATUSES_CANON)
  assignee   TEXT,                        -- lead | engineer | qa | founder | (null)
  priority   TEXT NOT NULL DEFAULT 'medium',  -- critical | high | medium | low
  pr_url     TEXT,
  notes      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project    TEXT NOT NULL,
  agent      TEXT NOT NULL,
  task_id    TEXT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at   TEXT,
  status     TEXT NOT NULL DEFAULT 'running',  -- running | succeeded | failed | capped
  log_path   TEXT,
  summary    TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project, started_at);

-- ordered migrations applied on top of this base schema; see harness/migrations/
CREATE TABLE IF NOT EXISTS schema_version (
  filename   TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
