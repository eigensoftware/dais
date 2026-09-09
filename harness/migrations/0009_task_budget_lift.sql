-- The founder's answer to a task past its spend ceiling (project.yaml task_max_runs /
-- task_max_tokens, plan 1.6): `dais task set <id> --budget-lift` stamps this, and the ceiling
-- counts only runs started AFTER the stamp — lifting resets the meter without a new state, so
-- the machine and its lint stay untouched. NULL = never lifted (every run counts).
ALTER TABLE tasks ADD COLUMN budget_lifted_at TEXT;
