-- A structured verdict (plan 3.4): `dais fire <task> <verb> --verdict '{...}'` stores the JSON a
-- reviewing role hands over with its transition (QA's pass/fail, a lead's recommendation) —
-- machine-readable for `dais brief`, `dais top`, and the change-request analytics, while the
-- rendered form still rides the notes log for the next reader. NULL = no verdict recorded.
ALTER TABLE tasks ADD COLUMN verdict TEXT;
