-- The run ledger: what each run consumed, as the provider reported it (fmt-stream's normalized
-- <log>.usage.json, stored by run-agent at the end of the run). input_tokens is the WHOLE prompt
-- across the run's turns (cache reads/writes included); cache_* break it down; cost_usd is the
-- provider's own dollar figure (claude's total_cost_usd — API-equivalent even on a subscription;
-- NULL for codex, which reports none); turns; session_id (claude, for resume). NULL = the run
-- reported nothing (died before a usage event) or predates this migration — never zero.
-- `dais cost` reads these. Runs before this migration have no usage: the logs discarded it.
ALTER TABLE runs ADD COLUMN input_tokens INTEGER;
ALTER TABLE runs ADD COLUMN output_tokens INTEGER;
ALTER TABLE runs ADD COLUMN cache_read_tokens INTEGER;
ALTER TABLE runs ADD COLUMN cache_write_tokens INTEGER;
ALTER TABLE runs ADD COLUMN cost_usd REAL;
ALTER TABLE runs ADD COLUMN turns INTEGER;
ALTER TABLE runs ADD COLUMN session_id TEXT;
