-- Recorded check results (plan 2.7): `dais check <task> [<check>]` runs the machine's declared
-- checks.<name> command in a throwaway worktree of the task's PR branch and records
-- {name: {"ok": 0|1, "at": ts, "pr": pr_url, "by": "dais check"}} here (JSON). A `verify:<name>`
-- guard honors a fresh record (same PR, < 24h) before running the command itself or falling
-- back to the firing role's --verify self-assertion — a deterministic test run costs zero
-- tokens and makes QA's attestation real. NULL = nothing recorded.
ALTER TABLE tasks ADD COLUMN check_results TEXT;
