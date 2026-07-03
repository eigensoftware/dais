-- Conditional release attests. The greenlight guard `attest:<fact> when task:touches_migrations`
-- demands the migrations attestation ONLY when the release's encompassed diff actually touches DB
-- migration files. The `assemble` step (engineer, in the repo) records this via
-- `dais task set <release> --touches-migrations true|false`; the pure guard checker then reads the
-- column. NULL = unknown / not-yet-computed → the attest is STILL required (fail-safe), so a missing
-- value can never silently skip the gate — only an explicit `false` lifts it.
ALTER TABLE tasks ADD COLUMN touches_migrations INTEGER;
