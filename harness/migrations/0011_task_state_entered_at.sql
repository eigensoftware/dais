-- When a task ENTERED its current state (plan 2.3). Gate age used to read tasks.updated_at,
-- which every `dais task set` bumps (a note, a priority, a title) — so annotating a 3-day-old
-- founder gate made it read as fresh in the vitals alarm and on its row. fire() stamps this on
-- every transition and create_task at birth; nothing else touches it. Backfilled from
-- updated_at (the best available guess for rows that predate it); readers fall back to
-- updated_at when NULL.
ALTER TABLE tasks ADD COLUMN state_entered_at TEXT;
UPDATE tasks SET state_entered_at = updated_at WHERE state_entered_at IS NULL;
