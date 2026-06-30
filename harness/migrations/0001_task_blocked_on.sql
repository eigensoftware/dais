-- Task dependencies. A task may be blocked on a predecessor (by task id). A task whose predecessor
-- is not yet done/cancelled is NOT schedulable: the router excludes it from a role's actionable
-- count (harness/router.py) and the panel marks it ⛓ blocked-on <id> (harness/panel.py). Cross-
-- project refs are allowed; a dangling ref (predecessor deleted) is treated as unblocked, never
-- stranded. Added by migration (not the base schema) so existing dais.db files pick it up via
-- `dais migrate`.
ALTER TABLE tasks ADD COLUMN blocked_on TEXT;
