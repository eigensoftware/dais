# Dais

**Preside over a crew of agents — they do the work, you approve what ships.**

Dais is a small, transparent harness for running an autonomous multi-agent org from the
command line. Agents (lead / engineer / qa / …) run as headless `claude -p` sessions,
coordinated through a SQLite board. You operate the whole thing with the `dais` CLI and gate
every outward action (publishing, merging, deploying).

It ships tuned for software teams, but the coordination is **domain-neutral**: a per-role
*playbook* carries the craft-specific conventions, so the same harness runs legal, research,
or content work just as well as code (see [Playbooks](#playbooks-running-any-craft)).

- **Source of truth:** one SQLite board (`dais.db`) — `tasks` + per-run `runs` history.
- **Roles-as-config:** each project declares who exists and how they're scheduled in a plain
  `roles` file; a status-driven router picks who runs next (reviewers → builders → planners).
  Adding a role is a persona file + one line — or let `dais role new` design it for you.
- **Playbooks:** working conventions are bound to the role, not baked into the universal prompt,
  so one harness spans many domains. The default is `code`.
- **Founder-gated:** gates frame the loop — `proposed` (front door: what gets built), a back-door
  deliverable gate (`ready_to_merge` for a QA-approved PR you merge, or `needs_review` for a finished
  non-code deliverable you close), and — where merge ≠ deploy — a manual **deploy** gate.
- **Just shell + SQLite + a little Python.** No heavy frameworks.

## Concepts: the tool vs. your workspace

Dais separates the **tool** (this repo — the `dais` binary and `harness/`) from your
**workspace** (a folder holding your `projects/` and the `dais.db` board). One installed
tool can drive any number of independent workspaces.

- **`DAIS_ROOT`** — where the tool's code lives (resolved automatically, even through a
  PATH symlink).
- **`DAIS_HOME`** — your workspace (where `projects/` + `dais.db` live). Resolved from the
  `DAIS_HOME` env var, else `~/.dais/config` (`home=/path/to/workspace`), else defaults to
  `DAIS_ROOT` (so a fresh clone runs self-contained).

## Install

```sh
git clone https://github.com/eigensoftware/dais ~/dais
mkdir -p ~/.local/bin && ln -s ~/dais/dais ~/.local/bin/dais   # put `dais` on your PATH (a pointer, not a copy)

dais init ~/my-workspace                     # bootstrap a workspace — board + dais.yaml + CONTEXT.md + projects/
mkdir -p ~/.dais && echo "home=$HOME/my-workspace" > ~/.dais/config   # make it your default DAIS_HOME
```

`~/.local/bin` must be on your `PATH`. If `dais` isn't found after the symlink, add it (then restart
the shell): `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`. Or symlink into a dir already on
your PATH (e.g. `/usr/local/bin`), or just call `~/dais/dais` directly.

`dais init` is the step that creates the workspace — without it you get a board and `projects/` but
no `dais.yaml` or workspace `CONTEXT.md` (the latter is injected into every agent run). It's idempotent,
so you can re-run it on an existing folder to backfill anything missing.

Prefer a **self-contained** install (tool and workspace in one folder)? Run `dais init ~/dais` and skip
the `~/.dais/config` line — with no config, `DAIS_HOME` falls back to the clone (`DAIS_ROOT`).

Update the tool any time with `git pull` in `~/dais` — the symlink reflects it instantly.

**Requirements:** `sqlite3`, `python3` (stdlib only), and the [Claude Code](https://claude.com/claude-code)
CLI (`claude`) for the agents. `gh` is needed for `dais ship`. Runs on macOS (bash 3.2+)
and Linux.

## Quickstart

```sh
dais scaffold myproject     # create a project from the template (edit its project.yaml + roles)
dais lint myproject         # validate the project config
dais top                    # live control panel — the primary way to operate the workspace
dais watch                  # run the loop: agents drain the queue, gating outward actions for you
```

## Driving the control panel (`dais top`)

`dais top` is the primary interface — a live, master–detail TUI you watch and act from. It refreshes
on an interval; nothing it does is destructive without a confirm.

**The panes:**

- **Vitals** (top bar) — `● running · ◆ NEED YOU · watch state · projects · clock`. Calm when nothing
  needs you; the **NEED YOU** token turns yellow when work is waiting on a founder gate.
- **PROJECTS** (rail) — a per-project table: `run · you · scp · que · bkl` (running · needs-you ·
  scoping · queued · backlog), plus an **ALL** row that totals across projects. The live project is
  green. Select a project to filter the WORK list to it; **ALL** clears the filter.
- **WORK** — the task list in bands: **RUNNING · NEEDS YOU · SCOPING · QUEUED · BACKLOG · DEFERRED ·
  ARCHIVE** (empty bands collapse to a dim header). A task blocked on an unfinished dependency shows
  `⛓` and dims. Any custom status auto-surfaces as its own band.
- **INSPECTOR** — detail of the selection: title, notes/spec, recent runs. Select a **running** agent
  and it streams that agent's **live log** here (wraps long lines; `j`/`k` scroll up into history).
- **FEED** — a one-line ticker of the most recent runs.

**Keys** — the bottom bar always shows the actions valid for the current selection, each with its
key; `?` opens the full map. Case is literal: a **capital** letter means Shift (e.g. `R`/`L`, because
`r`/`l` are already the runs view / log pager).

- **Navigate:** `tab` switch pane · `j`/`k` move selection or scroll · `g` expand backlog+archive ·
  `/` filter · `rail + j/k` pick a project.
- **Act on the selected task** (only the valid ones show): `a` advance (promote / start / approve /
  ship …) · `x` reverse (defer / cancel / reject …) · `s` scope · `h` handoff · `e` edit title ·
  `+`/`-` priority · `o` open PR · `n` new task · `↵` action menu (the same keys, listed).
- **Loop / run** (act on the selected row's project): `w` start/stop watch · `R` run a role now ·
  `t` tick once · `p` pause/resume · `c` cancel the running agent · `D` deploy (confirms) · `F` file
  a fix task for a failed deploy.
- **Views:** `r` runs history (every completed run, incl. task-less — `j`/`k` to a run, `l`/`↵` opens
  its saved log) · `l` log pager · `L` live log wall (all running agents).
- **Everywhere:** `q` quits (asks to confirm) from any screen; `esc` backs out of a view/overlay one
  level. `q` never just closes a screen — it always quits.

**Manual vs. the loop.** `dais watch` is the continuous auto-dispatcher (it's what `PAUSED` refers
to). `a start`, `R`, and `t` are **on-demand** runs that fire one agent now and **bypass pause** — so
you can kick off work while the loop is parked. `start` runs the *role* that handles the task's
status, which then pulls the **highest-priority** task of that status (not necessarily the one you
clicked). To make work flow automatically, **resume** (`p`) or start the loop (`w`).

## How work flows

Statuses are the spine — a task's status *is* whose turn it is, and the router runs the role that
owns it (verify → build → plan by precedence). A typical code task:

```
backlog ──▶ ready ──▶ needs_qa ──▶ ready_to_merge ──▶ done
  (you)    (engineer)   (qa)        (you merge)
              ▲            └─ bounce ──▶ changes_requested ──▶ (engineer)

a sparse task you add ──▶ needs_scoping ──▶ (lead specs it) ──▶ ready
                                                              └─▶ proposed  (you approve new direction)
```

Two founder gates frame it: **`proposed`** (front door — a Lead initiative you approve before it's
built) and the back-door deliverable gate — **`ready_to_merge`** (a QA-approved PR you merge) or
**`needs_review`** (a finished non-code deliverable you review and close). **Dependencies:**
`dais task set <id> --depends-on <other>` keeps a task out of the queue until its predecessor is
done — the scheduler skips it and the panel marks it `⛓`.

**Deploy** is a third gate for projects where **merge ≠ deploy** (e.g. an auto-merge project that
deploys manually). Set a `deploy:` command in `project.yaml` (any shell — ssh pull, `fly deploy`,
a script); `dais deploy <project>` (or `D` in the panel) runs it founder-gated and logs it as a run.
A migration-inclusive variant lives in `deploy_migrate:` — `D` auto-selects it (with a loud confirm)
when a pending commit touches a migration; deploys are always manual, never automatic.

**"Needs deploy?" is the truth, not a guess.** Set `deployed_rev:` — a command that prints what SHA
the *server* is running (e.g. `ssh … git rev-parse --short HEAD`). dais compares prod ↔ `main` and
shows **⬆ DEPLOY** (yes/no) plus an **AWAITING DEPLOY** band listing the exact commits that would
ship (select one for its full detail in the inspector) — so you never have to know prod's state
yourself. The panel refreshes that check in the background (`dais deploy <p> --check` caches it); a
successful deploy updates the cache. Projects where merge *is* the deploy (e.g. beacon) set no
`deploy:`.

Every deploy is **logged like a run** (full output saved under `projects/<p>/logs/`, openable from
the runs view). A failed deploy is surfaced loudly in the band (`⚠ last deploy FAILED …`) and leaves
prod behind, so it can't be missed; **`F`** files a fix task from it (founder-initiated — deploy
failures are often transient/ops, so nothing is auto-created).

## Playbooks: running any craft

The agent prompt is two layers: a **neutral coordination contract** (the board, statuses, hand-offs,
"do one unit then stop") that every agent gets, plus a **playbook** — the craft-specific conventions
for *how this kind of work is done here*. The tool ships `code`, `legal`, and `content`; add your own
under `harness/playbooks/` (or override per project in `projects/<name>/playbooks/`).

A playbook is bound at the **role** level, so a single project can mix crafts (an `engineer` on `code`,
a `gc` on `legal`, a `marketer` on `content`). Resolution is **role wins, project defaults**: the role's
6th `roles` column → the project's `playbook:` default → built-in `code`. So existing code projects need
no change, and a pure-legal workspace can set one default.

```
# roles:  name      access  trigger   handles          prec  playbook
gc          review  reactive  needs_legal      1     legal
engineer    edit    reactive  ready            3     code
marketer    draft   every:48h -               6     content
```

The full prompt an agent sees, in escalating specificity:
`workspace CONTEXT → project CONTEXT → role playbook → role persona`.

**Let Claude design a role:** `dais role new <project> --desc "what it does"` proposes a persona +
routing row (access / trigger / handles / playbook / prec) from your project's existing roles; you
confirm, and `dais lint` guards the invariant that each status has exactly **one** schedulable owner.
A role's `handles` is reactive ownership: a cadence role (e.g. a `lead` on `every:5h`) that also
`handles needs_scoping` reacts to scoping work on the next tick **and** keeps its periodic run.

## The CLI

| Command | What it does |
|---|---|
| `dais init [path]` | bootstrap a workspace (dais.yaml + CONTEXT.md + projects/ + board); idempotent |
| `dais status` | the dashboard — running now, merge-ready, blocked-on-you, queues, recent runs |
| `dais top [secs]` | the live control panel (see [Driving the control panel](#driving-the-control-panel-dais-top)) |
| `dais backlog <project>` | stage goal + ranked queue |
| `dais tasks <project>` | list a project's tasks (filter by `--status` / `--assignee`) |
| `dais watch [secs] [N]` | run the loop (N = parallel agents, 1–5) |
| `dais pause` / `dais resume` | park / un-park the loop |
| `dais tick [project]` | run one scheduling tick (the router picks who runs) |
| `dais run <project> <agent>` | run a specific agent now |
| `dais start <id>` | run the role that handles this task's status, right now (bypasses pause) |
| `dais cancel <project>` | stop the project's in-flight agent (marks the run interrupted) |
| `dais task add/set …` | manage the board (incl. `--depends-on <id>` to gate on a predecessor) |
| `dais handoff <id> <role>` | hand a task to a role (sets the status that role handles) |
| `dais approve <id>` | approve a `proposed` initiative → `ready` |
| `dais ship <project> <pr#>` | QA-gated, migration-aware squash-merge (founder gate) |
| `dais deploy <project> [--migrate] [--check]` | run the project's deploy (gate after merge); `--check` caches prod's live SHA |
| `dais scaffold <project>` | create a new project from the template |
| `dais role new <project> --desc "…"` | Claude designs a new role (persona + routing); you confirm |
| `dais lint [project]` | validate a project (roles + project.yaml + playbooks + required files) |
| `dais migrate` | apply pending DB migrations (run with the loop paused) |
| `dais schedule install [secs]` | background ticks (launchd on macOS, cron on Linux) |
| `dais learn <project> "…"` | append a durable decision/gotcha to the project's CONTEXT.md |
| `dais actions <id>` | list the founder actions valid for a task + the exact command for each |
| `dais logs <project> [N]` | recent runs + their saved log paths (open one from the panel's `r` view) |

## Layout

```
dais                  the CLI — your control panel and the agents' coordination interface
harness/
  dispatch.sh         scheduler: one tick = pick + run the next agent
  run-agent.sh        runs an agent headless (claude -p), streams + logs what it changed
  router.py           decides who runs next from each project's roles file; also `dais lint`
  dashboard.py        the data layer + the classic status/top renderers
  panel.py            the default `dais top` control panel (responsive mission-control cockpit)
  playbooks/          craft conventions injected per role (code, legal, content, …)
  lib.sh schema.sql   shared helpers + the board schema
  migrations/         ordered DB migrations (applied by `dais migrate` / first init)
  templates/          the `dais scaffold` project template
  tests/              the test suite (python -m pytest harness/tests/)
```

In a workspace, each project lives under `projects/<name>/`:
`project.yaml` (repo, model, stage goal, optional `playbook:` default + `deploy:` command), `roles` (who exists +
how scheduled + each role's playbook), `agents/*.md` (role personas), `CONTEXT.md` (project
memory agents read first), and optional `playbooks/` (project-specific craft overrides).

## License

MIT © Eigen Software LLC
