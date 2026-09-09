# Accounts: pooling, rotation, and per-account cap state

Status: approved (decisions below) · 2026-09-09 · dev plan 5.4 (spec first; no code in this commit)

## Why

Two facts from the workspace: the founder holds more than one subscription (two 5x Max plans
beat one 20x for throughput), and the cap gate now scopes by *provider* (1.1) while the auto
fallback swaps *models* (and, since 5.3, packs). Neither knows that a provider may have several
independent credentials. The July deliberation reached the shape: a first-class **account** is
`{provider, kind, credential}`; roles reference accounts; a tier can be a **pool** of equivalent
accounts; cap state lives on the account. This spec pins the config, the mechanics, and what is
deliberately out of scope.

## The account

```yaml
# ~/.dais/accounts.yaml  (user-level: credentials never live in a workspace)
accounts:
  max-a:   {provider: anthropic, kind: subscription, config_dir: ~/.dais/accounts/max-a}
  max-b:   {provider: anthropic, kind: subscription, config_dir: ~/.dais/accounts/max-b}
  api-1:   {provider: anthropic, kind: api,          key_env: ANTHROPIC_API_KEY_1}
  chatgpt: {provider: openai,    kind: subscription, config_dir: ~/.dais/accounts/chatgpt}
```

- `kind: subscription` → the CLI's own login, isolated per account by its config directory:
  `CLAUDE_CONFIG_DIR` for claude, `CODEX_HOME` for codex (both confirmed in the July thread;
  verify `CLAUDE_CONFIG_DIR` on the installed build during implementation). Each account is
  logged in once, by the founder, by hand: `dais account login max-b` runs the CLI's login with
  that directory set. dais never provisions subscriptions.
- `kind: api` → a metered key read from `key_env` (the same transport as `auth: api` today:
  process env, `~/.dais/env`, `$DAIS_HOME/.env`).
- An account belongs to exactly one provider pack. Unknown provider → lint error.

The two implicit accounts keep today's behavior: `anthropic` = the ambient claude login,
`openai` = the ambient codex login. A workspace with no accounts file changes nothing.

## Referencing accounts from a role

```yaml
---
account: max-a                 # one account (today's provider:/auth: become a shortcut for this)
---
---
account: pool:max              # a pool (defined below): rotate across equivalents
fallback_account: chatgpt      # the next tier down (5.3's fallback_provider/model generalized)
fallback_model: gpt-5.4
---
```

```yaml
# ~/.dais/accounts.yaml
pools:
  max: {members: [max-a, max-b], policy: least-recently-capped}
```

Resolution stays in `router.agent_setup`: `account` → provider, kind, credential; `provider:`
and `auth:` remain valid and map to the implicit account of that provider. `fallback_account`
subsumes `fallback_provider`. A `model_by_priority` tier can name an account too
(`critical=max-a:claude-fable-5`) — the same `account:model` form everywhere a model is named.

## Selection and rotation

A run resolves its account at launch:

1. A single account: use it.
2. A pool: pick the member with no live cap, by `policy`:
   - `least-recently-capped` (default): the member whose cap marker is oldest or absent;
     ties → round-robin by run count today. Spreads load so both windows drain evenly.
   - `round-robin`: strict alternation by run count.
   - `first-free`: members in declared order.
3. Every member capped → the pool is capped → the fallback tier (`fallback_account`), else the
   run is scored `capped` as today.

The run row records the account (`runs.account`, migration 0015) beside provider and model,
so `dais cost --by account` and the cap gate can read it.

## Cap state moves to the account

Today: `projects/<p>/.model-<role>.exhausted` (per project, role, model) and the dispatcher's
per-provider COOLING set from run rows. Both are wrong for pools: a cap on `max-a` says nothing
about `max-b`, and it applies to every project and role on `max-a`.

- Marker: `~/.dais/accounts/<account>.cooldown` = `<epoch> <model>`; written when a run on that
  account caps; TTL = the subscription window (~5h; per-account `window:` override); cleared by
  a later success on that account.
- The dispatcher's COOLING set becomes per *account*: a role whose resolved account (or every
  pool member) is cooling is withheld; a role with a free member runs. The provider-level set
  stays as the degenerate case for the implicit accounts.
- `dais status` / top / web name the cooling accounts (`COOLING max-a`), and `dais doctor`
  lists accounts, their login state (`claude --version` under the config dir; `codex login
  status` under `CODEX_HOME`), and their windows.

## Running under an account

`run-agent.sh` resolves the account before the pack loads and exports the credential the pack
needs: `CLAUDE_CONFIG_DIR=<config_dir>` or `CODEX_HOME=<config_dir>` for subscriptions, the
provider's key var from `key_env` for api accounts. Packs stay unchanged: they exec their CLI in
the environment they are given. The lean profile composes: `CLAUDE_CONFIG_DIR` is exactly the
per-agent config directory 1.3 anticipated, so an account directory can also hold a curated
`settings.json` and plugins for that account's runs.

## What changes, file by file

| Area | Change |
|---|---|
| `~/.dais/accounts.yaml` | new: accounts, pools (user-level; never in a workspace) |
| `router.agent_setup` | `account`, `fallback_account`; `provider:`/`auth:` map to implicit accounts; pools resolved to a member at launch (`--pick-account` seam) |
| `run-agent.sh` | export the credential; record `runs.account`; cap marker per account; fallback tiers by account |
| `dispatch.sh` | COOLING/BACKOFF keyed by account (provider = implicit account) |
| `board` / status / top / web | cooling accounts named; `dais cost --by account` |
| `dais account` | `list`, `login <name>` (runs the CLI's login under the directory), `status` |
| migration 0015 | `runs.account TEXT` |
| lint / doctor | unknown account, a pool with no free member ever logged in, a provider mismatch |

## Out of scope (deliberate)

- Provisioning or sharing subscriptions; OAuth device flows are the founder's, by hand.
- Per-account budgets (the daily budget stays workspace/project; add `--by account` reporting first).
- Weighted pools; spend-aware selection (least-cost) — a policy to add once the ledger shows it matters.

## Tests

Resolution (account → provider/kind/credential; pools; implicit accounts; the legacy keys);
selection under each policy with fake cap markers; the exported environment per kind (fake
CLIs asserting `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / the key var); cap marker per account and
the dispatcher's per-account COOLING via dry-run ticks; `runs.account` on the row; doctor's
account lines; migration on an old db.

## Decisions (founder, 2026-09-09)

1. Accounts live in `~/.dais/accounts.yaml`, a separate user-level file. `~/.dais/env` keeps keys only.
2. The default pool policy is `least-recently-capped`.
3. A cap falls back to another account on the same provider first, then crosses providers:
   the pool's other members, then `fallback_account`. A role's `fallback_*` tiers stay as
   authored after the same-provider members are exhausted.
