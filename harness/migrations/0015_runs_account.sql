-- Record which ACCOUNT each run launched on (plan 5.4: accounts.py — a named credential such
-- as `max-b`, or the provider's implicit account `anthropic`/`openai`), beside provider (0007)
-- and model (0006). The dispatcher's cap-cooldown gate keys on it so a cap on one subscription
-- withholds only the roles that have no free account; `dais cost --by account` reads it; the
-- round-robin pool policy counts runs per account from it. Pre-migration rows stay NULL and are
-- read as the provider's implicit account.
ALTER TABLE runs ADD COLUMN account TEXT;
