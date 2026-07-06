-- Record which model each run launched with (router.agent_setup's resolution), so run
-- history — e.g. an archived task's "runs touching" — can show WHO did the work with WHAT.
-- Pre-migration rows stay NULL and render as blank. Added by migration (not the base schema)
-- so existing dais.db files pick it up via `dais migrate`, per this repo's convention.
ALTER TABLE runs ADD COLUMN model TEXT;
