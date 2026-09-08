-- Record which PROVIDER each run launched on (router.agent_setup's resolution: anthropic |
-- openai | …), beside the model (0006). The dispatcher's cap-cooldown and error-backoff gates
-- read it to scope a cap to the provider that hit it: a Claude subscription window closing must
-- not park codex roles, and a codex rate limit must not park Claude (they were workspace-global).
-- Pre-migration rows stay NULL and are read as 'anthropic' — every historical run was Claude.
ALTER TABLE runs ADD COLUMN provider TEXT;
