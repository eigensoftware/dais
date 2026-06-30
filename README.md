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
dais status                 # the dashboard: queues, running agents, recent runs
dais watch                  # run the loop: one agent per tick, drains the queue
dais top                    # live mission-control TUI (press `m` for the focal-point skin)
```

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
confirm, and `dais lint` guards the one-reactive-role-per-status invariant.

## The CLI

| Command | What it does |
|---|---|
| `dais status` | the dashboard — running now, merge-ready, blocked-on-you, queues, recent runs |
| `dais top [secs]` | live master–detail TUI; `m` toggles the mission-control skin (focal-point vitals, status dots) |
| `dais backlog <project>` | stage goal + ranked queue |
| `dais watch [secs] [N]` | run the loop (N = parallel agents, 1–5) |
| `dais pause` / `dais resume` | park / un-park the loop |
| `dais tick [project]` | run one scheduling tick (the router picks who runs) |
| `dais run <project> <agent>` | run a specific agent now |
| `dais task add/set …` | manage the board |
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
  panel.py            the default `dais top` control panel (responsive; `m` = mission-control skin)
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
