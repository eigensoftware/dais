# Shared board: one board host, many workers

Status: draft for founder review · 2026-09-09 · dev plan 6.2 (spec first; no code in this commit)

## Why

One laptop holds one set of logins, one CPU, and one sleep schedule. The founder wants three
things from a second machine: more provider accounts (a second Mac with its own Claude and
ChatGPT logins, so pools drain in parallel), more parallel runs, and an always-on host that
keeps the loop alive while the laptop sleeps. The founder chose the shape on 2026-09-09:
**one board host, workers over ssh**, with placement **by account, then least loaded**.

## The invariant: state never leaves the board host

Today the board is one SQLite file with a 10-second busy timeout, the tick lock is a local
directory, and eleven kinds of runtime state live as files beside the db (`.paused`,
`.watch.pid`, `.tick.lock`, the watch and dispatch logs, per-role `.lock-<role>`, the
`.model-<role>.exhausted` and `.cadence-<role>` markers, `logs/`, and the user-level
per-account cap markers). None of it moves. The board host keeps the db, the loop, the tick
lock, every marker, and every log. `run-agent.sh` keeps running on the board host, end to end.

A **worker** is a compute-and-credential endpoint: a machine with the provider CLIs, its own
logins, and its own repo checkouts. The board host reaches it over ssh (Tailscale ssh or plain
ssh with keys). Exactly two things cross the wire:

1. The **provider CLI invocation** (the pack's `provider_run`) — launched on the worker, its
   JSONL stream piped back over ssh into `fmt-stream.py` on the board host, so the log, the
   usage sidecar, the cap detection, and the run row are the board host's, as today.
2. The agent's **`dais` calls** (`dais task show`, `dais fire`, `dais task set`, `dais learn`,
   …) — the worker's `dais` runs in *client mode* and execs the same command on the board host
   over ssh, with `DAIS_RUN_ID`, `DAIS_ACTOR`, and `DAIS_TASK_ID` forwarded, so attribution and
   the machine's guards are exactly what a local run gets. No new API surface: the full CLI
   answers, output streams back.

Nothing replicates. There is one writer, one dispatcher, one tick lock. SQLite over a network
filesystem (NFS, iCloud, Dropbox) is a known corruption path and is ruled out; a replicated
SQLite (rqlite, LiteFS) would add a daemon and a dependency to a stdlib-only harness for a
problem this shape does not have.

## Configuration

```yaml
# dais.yaml on the board host (workspace-level: workers serve THIS workspace)
workers:
  mini:                                   # an always-on Mac mini
    ssh: jack@mini.tail1234.ts.net        # anything `ssh <this>` accepts (Tailscale ssh, keys)
    dais_root: /Users/jack/dais           # the tool checkout on the worker (the client shim + hooks)
    agent_repos: /Users/jack/repos        # where the worker's checkouts live (lib.sh repo_path's base)
    accounts: [max-b, chatgpt]            # the named accounts LOGGED IN on this worker
    max_runs: 3                           # live runs this worker takes at once (default 2)
  laptop:
    ssh: jack@mbp.tail1234.ts.net
    dais_root: /Users/jack/Desktop/dais
    agent_repos: /Users/jack/Desktop
    accounts: [max-a]
```

- A worker's `accounts:` lists the named accounts (5.4) whose login lives there; an account's
  `config_dir` is a path on that worker. The implicit accounts (`anthropic`, `openai`, …) exist
  wherever the CLI does. An account named by no worker is logged in on the board host.
- Roles gain an optional `worker: mini` pin in frontmatter (project.yaml `worker_<role>` /
  `worker`), for the case the founder wants placement by hand. Absent = automatic.
- `dais worker add <name> --ssh <target> --dais-root <p> --agent-repos <p> [--accounts a,b]`
  writes the entry; `dais worker list` shows each worker's health, live runs, and accounts;
  `dais worker check <name>` runs the preflight (below).

## Placement: by account, then least loaded

The dispatcher already computes a role's account plan each tick (5.4, `--account-attempts`).
Placement reads it:

1. `worker:` pinned → that worker (health permitting; else the role is withheld and named).
2. The plan's first free account is listed by exactly one worker → that worker. Listed by none
   → the board host. (A pool spanning machines therefore lands each run where its chosen
   account is logged in, and a cap on `max-a` moves the next run to the worker holding `max-b`.)
3. Otherwise the candidate with the fewest live runs (board host included), ties → the board
   host, then declared order. A worker at `max_runs` is not a candidate.

The chosen worker rides the launch as `DAIS_WORKER=<name>`; the run row records it
(`runs.worker`, migration 0016; NULL = the board host). Live-run counts come from
`runs.status='running'` grouped by worker.

## Running on a worker

`run-agent.sh` gains one seam, `wexec`, that runs a command locally or over ssh on the
placed worker, with the environment the command needs:

```
wexec [ENV=val …] -- cmd args…    # local: exec; remote: ssh -o BatchMode=yes <target> env … cmd args…
```

- **Repo operations** (`git fetch`, the ff-only merge, worktree add/remove/prune, status,
  rev-parse) go through `wexec`. `REPO` and `WORKDIR` resolve against the worker's
  `agent_repos`. A missing checkout on the worker is a preflight failure, named.
- **The provider CLI**: each pack's `provider_run` already receives the prompt as arguments;
  remotely, the standing prompt and persona travel as a file over stdin (`ssh … 'cat > "$T"'`)
  to keep argv small and the shell quoting exact, then the CLI runs with the account's
  credential exported in the remote environment (`CLAUDE_CONFIG_DIR` / `CODEX_HOME` / the key
  var — the same `apply_account` values, applied remotely) and `--add-dir`/`--cd` pointed at
  the worker's `WORKDIR`. Its stdout is the JSONL stream; it pipes into the board host's
  `fmt-stream.py` unchanged. `ssh -tt` is not used (it would mangle the stream); instead the
  remote command runs under `setsid` with the run's marker in its argv, so cancel can find it.
- **The access hook** (`--settings` PreToolUse guard) references
  `<worker dais_root>/harness/hooks/guard.sh`.
- **The agent's `dais` calls**: the remote launch exports `DAIS_BOARD="<user@host>:<board
  dais path>:<DAIS_HOME>"` plus `DAIS_RUN_ID`, `DAIS_ACTOR`, `DAIS_TASK_ID`. The worker's
  `dais` (the same script, from `dais_root`) sees `DAIS_BOARD` and execs
  `ssh <user@host> env DAIS_RUN_ID=… DAIS_ACTOR=… DAIS_TASK_ID=… DAIS_HOME=… <board dais> "$@"`.
  Client mode covers every subcommand; nothing else on the worker touches a db.
- **Session resume** (3.2) keys on the session id the CLI reports; a resumed session must land
  on the same worker (the CLI's session store is local). The resume rule adds "same worker";
  otherwise it starts fresh, as a cross-provider attempt does today.
- **Caps and budgets**: the `max_minutes` watchdog kills the local ssh, and cancel
  (`dais cancel`) additionally runs `wexec pkill -f dais-run-<id>` on the worker. `--max-turns`
  / `--max-budget-usd` ride the argv unchanged.
- **Cap markers, model markers, cadence markers, logs, usage**: all written on the board host
  by the board host's `run-agent.sh`, exactly as today.

## Health and failure

- Each tick, the dispatcher probes every worker once (`ssh -o ConnectTimeout=5 … true`, cached
  for the tick). A worker that fails the probe is skipped for placement and named in the tick
  log ("worker laptop unreachable — placing elsewhere"). A sleeping laptop therefore loses no
  work; the board host or another worker takes the run.
- A run whose ssh dies mid-stream is scored `failed` (the log is what arrived), feeds the
  per-provider backoff as today, and the task stays put for the next tick.
- `dais worker check <name>`: ssh reachability, `dais_root` present and the same version as
  the board's, each provider CLI on the worker's PATH, each listed account's login
  (`login_status` under its config dir), each active project's repo present under
  `agent_repos`. `dais doctor` runs it for every worker.
- Viewing from another machine is `dais web` over Tailscale (plan 4.5), not a second `dais top`
  against a copied db. A worker never opens the db.

## What changes, file by file

| Area | Change |
|---|---|
| `dais.yaml` | `workers:` (ssh, dais_root, agent_repos, accounts, max_runs) |
| `router.py` | `workers()`, `place(root, project, role, plan, live_counts, health)`; `worker:` in agent_setup; lint: unknown worker pin, an account listed by two workers, a pool with no reachable member |
| `dispatch.sh` | per-tick worker health probe; placement per launch; `DAIS_WORKER` in the launch env; live counts per worker |
| `run-agent.sh` | `wexec`; repo ops, the pack's CLI, the account credential, and the hook path through it; `DAIS_BOARD` for the agent; `runs.worker`; cancel + watchdog reach the worker |
| `dais` | client mode (`DAIS_BOARD`); `dais worker add\|list\|check\|remove`; `dais cancel` reaches workers |
| `doctor.py` | every worker's preflight |
| `board.py` / top / web | the RUNNING band and the run row name the worker (`mini · engineer`) |
| migration 0016 | `runs.worker TEXT` |
| `cost.py` | `--by worker` |

## Out of scope (deliberate)

- Replicating the db, or running two dispatchers. One board host, by decision.
- Syncing workspace config to workers: the worker never reads `projects/`; every prompt is
  assembled on the board host and shipped with the launch.
- A worker on Windows. Linux and macOS workers only (ssh, `setsid`, `pkill`).
- Automatic repo provisioning on a worker: `git clone` is the founder's, once, by hand;
  `dais worker check` says what is missing.

## Tests

Placement (pinned; by account across workers; least loaded with ties; a full worker;
an unhealthy worker skipped) with fake health and counts; `wexec` local vs remote through a
fake `ssh` on PATH that records its argv and runs the command locally (so the whole
run-agent path — repo ops, the CLI with its exported credential, the stream, the hook path —
is asserted end to end); client mode forwarding the run identity; `runs.worker` on the row;
cancel reaching the fake worker; lint; doctor and `dais worker check` lines; migration on an
old db.

## Open questions for the founder

1. Should the worker's `dais` be the full tool checkout (`dais_root`, same version as the
   board, checked by doctor) or a single self-contained shim script that dais installs over
   ssh (`dais worker install <name>`)? The checkout is simpler to reason about; the shim is
   simpler to keep in sync.
2. When the placed worker is unreachable for a `worker:`-pinned role, withhold the role
   (proposed — the pin is a promise about where the login lives) or fall back to automatic
   placement?
3. Is Tailscale ssh the transport you run, or plain ssh with keys? (Only the preflight's
   wording depends on it.)
