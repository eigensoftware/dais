# Dais development plan — 2026-09

Status: proposed roadmap, from the 2026-09-07 review of the tool against the real
workspace (2,091 runs). Every item is test-first. Every phase ends with a milestone
the run ledger can measure. Effort is in focused developer days.

Conventions kept throughout: shell + SQLite + stdlib Python; guards stay honest
(no UI or automation may satisfy a typed confirm or an attest); every state change
fires an edge.

## Measured baseline (what the plan is fixing)

| Fact | Value |
|---|---|
| Baseline context per turn, personal Claude Code config | ~36K tokens |
| Same, settings and MCP stripped | ~23K tokens |
| Dais standing prompt | ~4K tokens |
| Largest CONTEXT.md files, read every run | 40KB showtime, 39KB counsel-os |
| Lead no-op rate: eigen-legal / counsel-os / lyrello | 87% / 78% / 75% |
| Typical QA run | 25–35 tool calls |
| Longest average run (voice/strategist) | 49 minutes, unbounded |
| Cost telemetry recorded by dais | none |

## Phase 0 — done

- Codex as a real per-role choice: honest failure on codex error events, provider CLI
  preflight, provider shown in `dais project` / `dais top`, `dais role new` asks,
  `--ephemeral` + closed stdin, lint warns on a missing CLI. Branch `codex-either-or`.

## Phase 1 — measure, and stop the bleeding (≈ 4.5 days)

Goal: every run has a cost; nothing runs unbounded; no-op cadence runs stop.

| # | Item | Effort | Depends on |
|---|---|---|---|
| 1.1 | Per-provider cap cooldown and error backoff (bug 1). Migration 0007 `runs.provider`; skip-not-halt in the eligible loop; cooling providers named in status/top | 0.5 | — |
| 1.2 | Run ledger + `dais cost`. Capture the result/usage events in fmt-stream (both providers); migration 0008 usage, cost, turns, session id; report per role, project, shipped task, no-op rate | 1 | 1.1 |
| 1.3 | Lean agent profile. `--setting-sources ''`, `--strict-mcp-config`, `--disable-slash-commands`; frontmatter `mcp:` allowlist. Measured −13K tokens per turn | 0.5 | — |
| 1.4 | Budget caps per role: `max_turns`, `max_budget_usd`, `max_minutes` → CLI flags + timeout wrapper | 0.5 | — |
| 1.5 | Harness-side idle check for cadence roles: board fingerprint unchanged since last run → skip, with a heartbeat | 0.5 | — |
| 1.6 | Daily loop budget (`dais watch --budget`) and per-task spend ceiling (auto-park in NEEDS YOU with the reason) | 0.5 | 1.2 |
| 1.7 | Top: "why idle" line from the tick journal; COOLING badge names the provider | 0.5 | 1.1 |
| 1.8 | Record the model a codex run actually used when `model:` is unset | 0.25 | — |

Milestone: `dais cost` shows spend per role; lead runs on unchanged boards are zero;
no run exceeds its caps.

## Phase 2 — dispatcher correctness and machine-enforced rules (≈ 4.5 days)

Goal: no wasted-run loops; rules that lived in prompts live in the machine.

| # | Item | Effort | Depends on |
|---|---|---|---|
| 2.1 | Dry-run must not mutate stall markers (bug 2); tick lock so watch/launchd/manual ticks cannot double-dispatch (bug 3) | 0.25 | — |
| 2.2 | Probe-loop cooldown, design option C: progress = net status diff after reconcile | 0.5 | — |
| 2.3 | `state_entered_at` stamped in `fire()`; gate age in top reads from it (bug 4) | 0.5 | — |
| 2.4 | Small fixes: env file trailing newline (bug 6); atomic lock claim on manual starts (bug 7); keep the capped attempt's log (bug 8); `then` effect matches on `by`, lint E7 (bug 9); one owner for priority order (bug 10) | 1 | — |
| 2.5 | Bounce limit as a machine guard: N QA fails route to a founder state | 0.25 | 2.3 |
| 2.6 | External-condition system guards: `checks.ci_green` polled at tick; QA dispatches only on green PRs | 0.5 | — |
| 2.7 | `dais check <task>`: run the machine's declared check in a worktree of the PR branch; stamp the result; `verify:` reads it | 0.5 | 2.6 |
| 2.8 | Duplicate task detection in `task add` (title similarity, warn + link) | 0.25 | — |
| 2.9 | `dais doctor`: CLI logins, keys, `gh auth`, repo state, schema version | 0.5 | — |
| 2.10 | Learn review queue: `dais learn` lands pending; founder promotes into CONTEXT.md from top (bug 5, and CONTEXT bloat) | 0.5 | — |

Milestone: a night of `dais watch` produces no repeated-task runs; QA never runs on a
red PR; every learn entry is attributed and reviewed.

## Phase 3 — fewer turns per run (≈ 3 days)

Goal: per-run token baseline down, measured by the ledger.

| # | Item | Effort | Depends on |
|---|---|---|---|
| 3.1 | Inline the deterministic first turns: task show + both CONTEXT files in the cached prompt prefix; hard cap on CONTEXT.md size | 0.5 | 2.10 |
| 3.2 | Session resume on the same role and task within a window (`--resume`, session id from the ledger) | 0.5 | 1.2 |
| 3.3 | Effort and model tiers per role; `model_by_priority` | 0.5 | 1.2 |
| 3.4 | Structured verdicts: `--json-schema` for QA and lead output; parsed verdict block in notes | 0.5 | — |
| 3.5 | Throughput: parallel width default with worktree isolation on more roles; `worktree_setup` dependency cache | 0.5 | — |
| 3.6 | Quiet hours for cadence roles | 0.25 | 1.5 |

Milestone: median tokens per QA run and per engineer run down by a measured share
against the Phase 1 baseline.

## Phase 4 — the founder loop (≈ 8 days)

Goal: decide from one screen, from anywhere.

| # | Item | Effort | Depends on |
|---|---|---|---|
| 4.1 | `dais brief <task>`: decision packet for a gate (encompassed PRs, diff stats, QA verdicts, migrations flag, last notes) | 1 | 3.4 |
| 4.2 | Change-request rate per gate and per role (`dais retro`); yolo candidates surfaced | 0.5 | 1.2 |
| 4.3 | Top batch: PR facts in the inspector; batch actions on NEEDS YOU; machine board view; notes rendering + note search; cost columns; log wall filters; collision warning; next-tick preview | 2 | 1.2 |
| 4.4 | Push notifications when NEEDS YOU lights up (telegram) | 0.5 | — |
| 4.5 | `dais web [port]`: stdlib server over `board.load_snapshot`, one HTML file, SSE live updates, actions via `dais fire` (engine-enforced guards), localhost + launch token | 3 | 3.4 |
| 4.6 | Web charts: machine diagram with live counts, run timeline, cost over time, change-request rates | 1 | 4.5, 1.2 |

Milestone: a gate is decided from the brief alone; gating works from a phone over
Tailscale.

## Phase 5 — providers and accounts (≈ 6 days, spec first)

Goal: any role on any provider, model, and account, with fallback.

| # | Item | Effort | Depends on |
|---|---|---|---|
| 5.1 | Provider packs: `harness/providers/<name>/` with a fixed contract (run, stream map, cap patterns, meta); adapters move out of run-agent.sh | 1 | — |
| 5.2 | Codex `model_provider` / `base_url` passthrough: OpenRouter, Groq, DeepSeek, vLLM, Ollama with no new adapter | 0.25 | 5.1 |
| 5.3 | Cross-provider fallback: attempts become (provider, model, account) tuples; marker keyed by account | 1 | 1.1, 5.1 |
| 5.4 | Account model: {provider, kind, credential or config dir}; pools with least-recently-capped rotation; cap state per account. `CLAUDE_CONFIG_DIR` / `CODEX_HOME` per account (shares the mechanism of 1.3) | 2 | 5.3 |
| 5.5 | opencode pack as the universal adapter | 1 | 5.1 |
| 5.6 | Access enforcement by hooks (block push/merge for review roles) on both providers | 0.5 | 5.1 |

Milestone: a Claude cap on one account moves the run to the next account or provider
with no founder action.

## Phase 6 — scale (later, spec first)

| # | Item | Effort |
|---|---|---|
| 6.1 | Multi-spawn effects with `blocked_on` chains: one approval fans out an initiative | 0.5 |
| 6.2 | Shared board across machines: replicated `dais.db`, distributed tick lock | 3+ |

## Sequencing rules

1. Phase 1 before everything: nothing later is measurable without the ledger.
2. 1.1 first: codex roles are live now and a Claude cap parks them.
3. 4.5 (web) waits for 3.4 (structured verdicts) and 1.2 (ledger); its value is the
   charts and the brief, not another list view.
4. 5.4 (accounts) gets a written spec before code; it is the one architectural item.
5. Every fix ships with the failing test that reproduces it.

Total: about 26 developer days, plus the two specs.
