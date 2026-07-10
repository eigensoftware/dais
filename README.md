# Dais

**Preside over a crew of agents: they do the work, you approve what ships.**

Dais is a small, transparent harness for running an autonomous multi-agent org from the
command line. Agents (lead / engineer / qa / …) run as headless CLI sessions (`claude -p`, or
`codex exec` for roles you put on OpenAI), coordinated through a SQLite board. You operate the
whole thing with the `dais` CLI and gate every outward action (publishing, merging, deploying).

It ships tuned for software teams, but the coordination is **domain-neutral**: each project
runs an **authored state machine** for its lifecycle and a per-role *playbook* for the
craft-specific conventions, so the same harness runs legal, research, or content work just as
well as code (see [Playbooks](#playbooks-running-any-craft)).

- **Source of truth:** one SQLite board (`dais.db`): `tasks` + per-run `runs` history + the
  composition graph (`task_links`: what spawned what, what a release encompasses).
- **Authored lifecycles:** each project owns a `machine.json`: states, edges, guards. Nothing
  pokes a status; **every state change fires an edge** (`dais fire <task> <verb>`), and the
  scheduler dispatches whichever role the machine names for a task's state.
- **Founder-gated:** guards make outward edges *structurally un-automatable*: a
  `typed_confirm` or `attest:<fact>` can only be satisfied by a human typing it. Two gates
  frame the loop: proposal approval (what gets built) and the release greenlight (what ships).
- **Just shell + SQLite + a little Python.** No heavy frameworks.

## Concepts: the tool vs. your workspace

Dais separates the **tool** (this repo: the `dais` binary and `harness/`) from your
**workspace** (a folder holding your `projects/` and the `dais.db` board). One installed
tool can drive any number of independent workspaces.

- **`DAIS_ROOT`:** where the tool's code lives (resolved automatically, even through a
  PATH symlink).
- **`DAIS_HOME`:** your workspace (where `projects/` + `dais.db` live). Resolved from the
  `DAIS_HOME` env var, else `~/.dais/config` (`home=/path/to/workspace`), else defaults to
  `DAIS_ROOT` (so a fresh clone runs self-contained).

## Install

**1. Get the `dais` CLI** (Homebrew, recommended, or from source).

Homebrew:

```sh
brew install eigensoftware/dais/dais       # or: brew tap eigensoftware/dais && brew install dais
```

Update later with `brew upgrade eigensoftware/dais/dais`.

From source (a clone you can hack on):

```sh
git clone https://github.com/eigensoftware/dais ~/dais
mkdir -p ~/.local/bin && ln -s ~/dais/dais ~/.local/bin/dais   # put `dais` on your PATH (a pointer, not a copy)
```

`~/.local/bin` must be on your `PATH`. If `dais` isn't found after the symlink, add it (then restart
the shell): `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`. Or symlink into a dir already on
your PATH (e.g. `/usr/local/bin`), or just call `~/dais/dais` directly. Update any time with `git pull`
in `~/dais`; the symlink reflects it instantly.

**2. Bootstrap a workspace:**

```sh
dais init ~/my-workspace                     # board + dais.yaml + CONTEXT.md + projects/
mkdir -p ~/.dais && echo "home=$HOME/my-workspace" > ~/.dais/config   # make it your default DAIS_HOME
```

`dais init` is the step that creates the workspace: without it you get a board and `projects/` but
no `dais.yaml` or workspace `CONTEXT.md` (the latter is injected into every agent run). It's idempotent,
so you can re-run it on an existing folder to backfill anything missing.

Prefer a **self-contained** install from source (tool and workspace in one folder)? Run `dais init ~/dais`
and skip the `~/.dais/config` line: with no config, `DAIS_HOME` falls back to the clone (`DAIS_ROOT`).

**Requirements:** `sqlite3`, `python3` (stdlib only), and the [Claude Code](https://claude.com/claude-code)
CLI (`claude`) for the agents. Add OpenAI's [`codex`](https://github.com/openai/codex) CLI if any role
sets `provider: openai`, and `gh` is recommended (the coding playbook has agents open PRs with it).
`brew install` alone won't run agents; those CLIs are what actually drive them. Runs on macOS
(bash 3.2+) and Linux.

## Quickstart

```sh
dais scaffold myproject     # create a project from a template (agents/ cast + its own machine.json)
dais lint myproject         # validate the project config + machine coherence
dais top                    # live control panel: the primary way to operate the workspace
dais watch                  # run the loop: agents drain the queue, parking at your gates
```

## The machine: how work flows

Each project owns its lifecycle as an authored state machine:
`projects/<name>/machine.json`, seeded from a stock template by `dais scaffold` and yours to
edit. Two templates ship with the tool: **coding** (the full propose, build, QA, release
lifecycle below) and **marketing** (a content lane: draft, review, founder publish). Start
from one, then edit your copy; the machine is data, not code. States are the board's bands;
**edges own every transition**: a verb, the role that may fire it (`by`), optional guards,
and optional effects. Nothing writes a status directly; every change is
`dais fire <task> <verb>`, and `dais edges <task>` shows what's fireable from where a task
sits. (Full model: [`design/machine-model.md`](design/machine-model.md).)

The stock **coding** machine:

```
proposed ──submit──▶ proposal_review ──approve ◆──▶ spawns the build task, ready
   │ (lead specs it)    (front-door founder gate;  request_changes bounces it back)
   └──promote──▶ ready   (the routine lane: bugfixes, QA cleanups, and follow-ups of
                          already-approved work skip the gate; only NEW direction gates)

ready ──claim──▶ doing ──complete──▶ qa_review ──pass ✓tests──▶ approved (parked for a release)
   │                                     └──fail──▶ blocked + a spawned fix task;
   │                                                auto-returns to qa_review when the fix lands
   └──needs_design──▶ design ──design_done──▶ ready (spec in hand; review-only
                        tasks close via review_done, findings in the notes)

release_open ──assemble──▶ release_review ──greenlight ◆──▶ releasing ──shipped──▶ done
  (aggregates every        (back-door founder gate;         (the release and everything
   approved task)           typed confirm + attests)         it encompasses close together)
```

Plus founder parking (`defer` / `undefer` returns a task to where it was parked from), an
engineer self-retire edge (`invalidate`), and a rollback lane (`release_error` spawns a fix,
founder `retry`s or `give_up`s).

Why `promote` is safe where it is, and the rule if you author your own edges: it is
**inward-only**. Everything the routine lane feeds still passes QA and still cannot ship
without the founder's release greenlight. Routine-*inward* work can self-serve; anything
*outward* (publish, deploy, send, spend) stays a founder gate no matter how routine it looks.
Do not copy the promote pattern onto an outward edge.

**Guards** are the gate mechanism, declared per edge:

| Guard | Satisfied by |
|---|---|
| `confirm` | a click (`--confirm`), weak |
| `typed_confirm` | a human typing the task id, **strong human** |
| `attest:<fact>` | a human asserting an unverifiable fact, **strong human**. Conditional form `attest:<fact> when task:<flag>` is required unless the task's flag is explicitly false (fail-safe: unknown still gates) |
| `verify:<check>` | the machine's declared `checks.<check>` command passing (or the firing role's explicit `--verify` self-assertion); fails closed |

**Effects** keep composition inside the machine: an edge can `spawn` a task (QA-fail spawns
the fix), `aggregate` a set (a release pulls in every `approved` task), or `then`-fire edges
on what it encompasses (shipping a release closes its children); each nested change still a
real, guarded edge. `fire` is atomic: a transition and all its effects commit or roll back
together.

**Dispatch is derived.** The scheduler runs the role the machine names for the top pending
task's state; cadence roles (e.g. a lead on `every:24h`) also run on their clock for periodic
discovery. `founder` edges are never dispatched; that work parks in **NEEDS YOU** until you
act. `dais lint` checks machine coherence (referential integrity, no dead ends, unambiguous
dispatch, reachability) and warns on outward edges with no strong-human guard on their
approach.

## Driving the control panel (`dais top`)

`dais top` is the primary interface: a live, master/detail TUI you watch and act from.
Guarded edges prompt **in the panel** with the same strength as the CLI flags (you type the
task id / the fact name to greenlight a release); nothing outward fires from a bare keypress.

**The panes:**

- **Vitals** (top bar): `● running · ◆ NEED YOU · watch state · projects · clock`. Calm when
  nothing needs you; **NEED YOU** lights up when work waits on a founder gate.
- **PROJECTS** (rail): per-project counts, plus an **ALL** row summarizing the workspace.
  Select a project to filter the WORK list; **ALL** clears the filter.
- **WORK**: tasks in machine-derived bands: **RUNNING · NEEDS YOU · QUEUED · WAITING ·
  ARCHIVE**. A task blocked on an unfinished dependency shows `⛓` and dims.
- **INSPECTOR**: the selection's detail: title, notes, recent runs, `next:` (its fireable
  edges + guards ◆) and `links:` (spawned-from / blockers / what a release encompasses).
  Select a **running** agent and it streams that agent's live log.
- **FEED**: a one-line ticker of the most recent runs.

**Keys:** the bottom bar shows the actions valid for the current selection (derived from the
machine's edges), each with its key; `?` opens the full map. Highlights: `tab` switch pane ·
`j`/`k` move/scroll · `/` filter · `↵` action menu · `n` new task · `e` edit title · `+`/`-`
priority · `o` open PR · `w` start/stop watch · `p` pause/resume · `t` tick · `R` run a role
now · `c` cancel the running agent · `C` cut a release · `P` project setup · `r` runs history ·
`l` log pager · `L` live log wall · `q` quits (with confirm) · `esc` backs out one level.

**Manual vs. the loop.** `dais watch` is the continuous auto-dispatcher. `dais start <id>`,
`R`, and `t` are on-demand runs that fire one agent now and bypass pause. `start` runs the
role the machine dispatches for the task's state, honoring the dependency chain.

## Playbooks: running any craft

The agent prompt is two layers: a **neutral coordination contract** (the board, the machine,
hand-offs, "do one unit then stop") that every agent gets, plus a **playbook**: the
craft-specific conventions for *how this kind of work is done here*. The tool ships `code`,
`legal`, `content`, and `plan`; add your own under `harness/playbooks/` (or override per
project in `projects/<name>/playbooks/`).

A playbook is bound at the **role** level, so a single project can mix crafts (an `engineer`
on `code`, a `marketer` on `content`). Resolution is **role wins, project defaults**: the
role's own frontmatter `playbook:` → the project's `playbook:` default → built-in `code`.

Each role's config (model, effort, provider, auth, scheduling) lives in its own
`agents/<role>.md`, as a flat `key: value` block between leading `---` markers, above the
persona prose:

```
---
provider: openai
auth: subscription
trigger: every:48h
prec: 60
playbook: content
---
# Marketer · myproject

You draft social copy and blog posts from the stage goal...
```

(A bare persona file with no frontmatter is fine: every key falls back down the chain; see
[Providers](#providers-anthropic--openai) below.) `access` is the exception: it's not a
frontmatter key; it's owned by `machine.json`'s `roles` block (see
[machine-model.md](design/machine-model.md#roles)), since it's what `run-agent` enforces.

**Role concurrency (`concurrency: N`, default 1):** how many runs of this role may live at
once. By default every project runs ONE agent at a time (the repo is shared state); a role
you declare `concurrency: 2..5` on may STACK — the dispatcher launches another run of the
*already-live role* when it has slot headroom and there are more dispatchable tasks than live
runs. It never launches a *second role* into a busy repo, and it never stacks onto a single
task. Turn it up only where runs are truly independent per task (content drafting, review-only
roles). Two cautions: roles whose agents run a shared test stack (a scratch DB) will collide —
isolate that first; and roles whose agents *drain a queue* may duplicate the top task's work
(the machine's compare-and-swap edges make duplicates harmless, but the spend is wasted).
Cadence roles (`every:Nh`) should stay at 1 — they groom shared state; `dais lint` warns.

**Let Claude design a role:** `dais role new <project> --desc "what it does"` proposes a
persona + config from your project's existing roles; you confirm.

## Providers: anthropic + openai

Each agent runs against one of two provider CLIs, chosen per-role:

```
---
model: gpt-5.1-codex-mini
provider: openai        # anthropic (default, claude CLI) | openai (codex exec)
auth: api                # subscription (default, CLI login) | api (metered)
---
```

Resolution (frontmatter → legacy roles file → `project.yaml` → defaults) is one authority,
`router.agent_setup`, read by every consumer (the scheduler, `run-agent.sh`, `dais project`).
**Model keys are provider-scoped:** a project-wide `model:` in `project.yaml` only applies to
roles resolved to that project's *default* provider; it never leaks a `claude-opus-4-8` id
onto a role you've overridden to `provider: openai` (or vice versa). Give a per-role override
its own `model:` in that role's frontmatter instead.

**`auth: api`** reads the provider's standard key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) from
the process environment, then `~/.dais/env`, then `$DAIS_HOME/.env` (workspace override); first
one set wins. Never put a key in `project.yaml` or a persona file (`dais lint` warns on
secret-shaped values); `dais init` gitignores `.env` for you. `auth: subscription` (the default)
runs the CLI as already logged in, nothing to configure.

**How codex roles are sandboxed, and why `edit` roles bypass it:**
- **`edit` roles run codex with its sandbox disabled** (`--dangerously-bypass-approvals-and-sandbox`).
  Codex's `workspace-write` sandbox blocks writes under `.git/`, which breaks an engineer's core
  job: commit, branch, PR. Disabling it is deliberate trust *parity*, not an escalation: an
  anthropic `edit` role already runs with `--permission-mode bypassPermissions`. On both
  providers, the protection for an edit role is the machine's guards and the founder gates
  (nothing outward ships without you), not a filesystem sandbox.
- **`review`/`draft` roles keep codex's write sandbox** (repo + the workspace, so `dais fire`
  works). But codex has no per-tool disallows like claude's: a reviewer on `openai` can't be
  made read-only-on-code the way an anthropic reviewer is (`--disallowedTools Edit Write …`).
  The sandbox plus the persona plus the machine's guards are the guard. If structurally
  read-only reviewers matter to you, keep those roles on `anthropic`.

## The CLI

| Command | What it does |
|---|---|
| `dais init [path]` | bootstrap a workspace (dais.yaml + CONTEXT.md + projects/ + board); idempotent |
| `dais status` | everything at a glance: running now, gates waiting on you, queues, recent runs |
| `dais top [secs]` | the live control panel (see [Driving the control panel](#driving-the-control-panel-dais-top)) |
| `dais project <name>` | a project's setup: cast + models, machine dispatch map, config |
| `dais tasks <project>` | list a project's tasks (filter by `--status` / `--assignee`) |
| `dais task add/set …` | manage the board (new tasks enter at the machine's entry state) |
| `dais fire <id> <verb>` | advance a task by firing a machine edge (guards: `--confirm` / `--typed` / `--attest` / `--verify`) |
| `dais edges <id>` | the fireable edges from a task's current state |
| `dais start <id>` | run the role the machine dispatches for this task's state, now (bypasses pause) |
| `dais watch [secs] [N]` | run the loop (N = parallel agents) |
| `dais pause` / `dais resume` | park / un-park the loop |
| `dais tick [project]` | run one scheduling tick (the machine picks who runs) |
| `dais run <project> <agent>` | run a specific agent now |
| `dais cancel <project>` | stop the project's in-flight agent (marks the run interrupted) |
| `dais scaffold <project> [--template coding\|marketing]` | create a project (agents/ cast + its own machine) from a template |
| `dais role new <project> --desc "…"` | Claude designs a new role (persona + routing); you confirm |
| `dais lint [project]` | validate a project (roles + project.yaml + playbooks + machine coherence) |
| `dais migrate` | apply pending DB migrations (run with the loop paused) |
| `dais migrate --config <project>` | convert a project's legacy roles file into `agents/<role>.md` frontmatter + machine-owned access |
| `dais schedule install [secs]` | background ticks (launchd on macOS, cron on Linux) |
| `dais learn <project> "…"` | append a durable decision/gotcha to the project's CONTEXT.md |
| `dais logs <project> [N]` | recent runs + their saved log paths |
| `dais version` | which build this machine runs |

## Layout

```
dais                  the CLI: your control panel and the agents' coordination interface
harness/
  machine.py          the engine: load/lint a machine, derive dispatch + bands, fire edges (atomic)
  dispatch.sh         scheduler: one tick = pick + run the next agent
  run-agent.sh        runs an agent headless (provider adapter: claude -p or codex exec), streams + logs what it changed
  router.py           agent_setup(): the one config-resolution authority (frontmatter → legacy roles file → project.yaml → defaults); cast + lint
  migrate_config.py   `dais migrate --config`: mechanical conversion of a project's legacy roles file into frontmatter + machine-owned access
  dashboard.py        data layer + plain renderers (status/project) + the base TUI action engine
  panel.py            the `dais top` cockpit: the multi-pane renderer on top of dashboard.py
  machines/           the machine templates projects are seeded from (coding, marketing)
  playbooks/          craft conventions injected per role (code, legal, content, plan)
  lib.sh schema.sql   shared shell helpers + the board schema
  migrations/         ordered DB migrations (applied by `dais migrate` / first init)
  templates/          the `dais scaffold` project templates
  tests/              the test suite (python3 -m pytest harness/tests/)
design/
  machine-model.md    the machine model: schema, guard vocabulary, effects, lint rules
```

In a workspace, each project lives under `projects/<name>/`:
`machine.json` (its authored lifecycle, and now the `roles` block that's authoritative for
`access`), `project.yaml` (repo, stage goal, optional project-wide `playbook:`/`provider:`
defaults), `agents/*.md` (the cast: persona prose plus each role's own frontmatter: model,
effort, provider, auth, trigger, prec, playbook), `CONTEXT.md` (project memory agents read
first), and optional `playbooks/` (project-specific craft overrides). The legacy `roles` file
(one row per role: name/access/trigger/handles/prec/playbook) still works this release but is
retired; `dais migrate --config <project>` converts it.

## License

MIT © Eigen Software LLC
