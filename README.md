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
- **Founder-gated:** two gates frame the loop — `proposed` (front door: what gets built) and a
  back-door deliverable gate: `ready_to_merge` for a QA-approved PR you merge, or `needs_review`
  for a finished non-code deliverable you review and close.
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
ln -s ~/dais/dais ~/.local/bin/dais          # put `dais` on your PATH (a pointer, not a copy)

# point it at a workspace (or skip this to run self-contained inside the clone)
mkdir -p ~/.dais && echo "home=$HOME/my-workspace" > ~/.dais/config
```

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
  `t` tick once · `p` pause/resume · `c` cancel the running agent.
- **Views:** `r` runs history (every completed run, incl. task-less) · `l` log pager · `L` live log
  wall (all running agents) · `q` quit (confirms).

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
| `dais status` | the dashboard — running now, merge-ready, blocked-on-you, queues, recent runs |
| `dais top [secs]` | live master–detail control panel — focal-point vitals, status dots, collapse-when-empty bands, outlined inspector |
| `dais backlog <project>` | stage goal + ranked queue |
| `dais watch [secs] [N]` | run the loop (N = parallel agents, 1–5) |
| `dais pause` / `dais resume` | park / un-park the loop |
| `dais tick [project]` | run one scheduling tick (the router picks who runs) |
| `dais run <project> <agent>` | run a specific agent now |
| `dais start <id>` | run the role that handles this task's status, right now (bypasses pause) |
| `dais task add/set …` | manage the board (incl. `--depends-on <id>` to gate on a predecessor) |
| `dais handoff <id> <role>` | hand a task to a role (sets the status that role handles) |
| `dais approve <id>` | approve a `proposed` initiative → `ready` |
| `dais ship <project> <pr#>` | QA-gated, migration-aware squash-merge (founder gate) |
| `dais scaffold <project>` | create a new project from the template |
| `dais role new <project> --desc "…"` | Claude designs a new role (persona + routing); you confirm |
| `dais lint [project]` | validate a project (roles + project.yaml + playbooks + required files) |
| `dais migrate` | apply pending DB migrations (run with the loop paused) |
| `dais schedule install [secs]` | background ticks (launchd on macOS, cron on Linux) |
| `dais learn <project> "…"` | append a durable decision/gotcha to the project's CONTEXT.md |
| `dais logs <project>` | tail the project's run logs |

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
`project.yaml` (repo, model, stage goal, optional `playbook:` default), `roles` (who exists +
how scheduled + each role's playbook), `agents/*.md` (role personas), `CONTEXT.md` (project
memory agents read first), and optional `playbooks/` (project-specific craft overrides).

## License

MIT © Eigen Software LLC
