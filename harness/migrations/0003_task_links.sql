-- Task composition graph (Graph 2 in design/machine-model.md). Tasks are the only atom; they compose
-- recursively. This records task<->task edges produced by transition EFFECTS (spawn/aggregate):
--   spawned_from  — child was spawned by parent (a proposal's impl tasks; a QA fail's fix task)
--   blocks_parent — child must reach a terminal before parent's `unblocked` guard passes
--   encompasses   — parent batches child (a release task over the approved set)
--   part_of       — generic aggregation
-- Kept separate from tasks.blocked_on (migration 0001, single-predecessor): a task can be blocked by
-- or encompass MANY. Added as a migration so existing dais.db files pick it up via `dais migrate`.
CREATE TABLE IF NOT EXISTS task_links (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id TEXT NOT NULL,
  child_id  TEXT NOT NULL,
  rel       TEXT NOT NULL,
  at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_links_parent ON task_links(parent_id, rel);
CREATE INDEX IF NOT EXISTS idx_task_links_child  ON task_links(child_id, rel);
