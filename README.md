# Dais

**Preside over a crew of coding agents — they build, you approve what ships.**

Dais is a small, transparent harness for running an autonomous multi-agent software
org from the command line. Agents (lead / engineer / qa / …) run as headless
`claude -p` sessions, coordinated through a SQLite board. You operate the whole thing
with the `dais` CLI and gate every outward action (publishing, merging, deploying).

- **Source of truth:** one SQLite board (`dais.db`) — `tasks` + per-run `runs` history.
- **Roles-as-config:** each project declares who exists and how they're scheduled in a
  plain `roles` file; a status-driven router picks who runs next (reviewers → builders →
  planners). Adding a role is a persona file + one line — no code changes.
- **Founder-gated:** two gates frame the loop — `proposed` (front door: what gets built)
  and `ready_to_merge`/deploy (back door: what goes live). Agents draft and propose; you
  approve.
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
dais top                    # live mission-control TUI
```

## The CLI

| Command | What it does |
|---|---|
| `dais status` | the dashboard — running now, merge-ready, blocked-on-you, queues, recent runs |
| `dais top [secs]` | live master–detail TUI with a colour-coded, scrollable per-agent log |
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
| `dais lint [project]` | validate a project (roles + project.yaml + required files) |
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
  dashboard.py        the data layer + both renderers (status one-shot and the top TUI)
  lib.sh schema.sql   shared helpers + the board schema
  migrations/         ordered DB migrations (applied by `dais migrate` / first init)
  templates/          the `dais scaffold` project template
  tests/              the test suite (python -m pytest harness/tests/)
```

In a workspace, each project lives under `projects/<name>/`:
`project.yaml` (repo, model, stage goal), `roles` (who exists + how scheduled),
`agents/*.md` (role personas), `CONTEXT.md` (project memory agents read first).

## License

MIT © Eigen Software LLC
