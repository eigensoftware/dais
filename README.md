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
edit. Four templates ship with the tool, distilled from workspaces we actually run:

| Template | Cast | Shape |
|---|---|---|
| **coding** | lead, engineer, designer, qa | the full lifecycle below: propose, build, QA, batched founder-gated releases |
| **lean** | engineer | the smallest start: you file work, one builder PRs it, you review and merge; grow it toward coding when you need more |
| **advisory** | analyst | decisions, not code: raise a question, get options + tradeoffs + one recommendation; zero outward edges by design |
| **marketing** | strategist, writer, editor | a content lane: draft, review, founder publish |

Start from one, then edit your copy; the machine is data, not code. States are the board's bands;
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
engineer self-retire edge (`invalidate`), a rollback lane (`release_error` spawns a fix,
founder `retry`s or `give_up`s), and a **bounce limit** on QA's `fail`: the third fail on one
task lands in `escalated` (NEEDS YOU: `resume` with your direction in the note, or `cancel`)
instead of spawning yet another fix. The machine enforces it, not a prompt.

Why `promote` is safe where it is, and the rule if you author your own edges: it is
**inward-only**. Everything the routine lane feeds still passes QA and still cannot ship
without the founder's release greenlight. Routine-*inward* work can self-serve; anything
*outward* (publish, deploy, send, spend) stays a founder gate no matter how routine it looks.
Do not copy the promote pattern onto an outward edge.

**QA only runs on a green PR.** The coding machine's `qa_review` state carries
`dispatch_when: verify:ci_green`: each tick runs the machine's `checks.ci_green` command for
the task (its PR in `$DAIS_PR`) and withholds it while a check fails or is pending, journaling
why. A repo with no CI still dispatches. Any state can carry a `dispatch_when`
([machine-model.md](design/machine-model.md)).

**Guards** are the gate mechanism, declared per edge:

| Guard | Satisfied by |
|---|---|
| `confirm` | a click (`--confirm`), weak |
| `typed_confirm` | a human typing the task id, **strong human** |
| `attest:<fact>` | a human asserting an unverifiable fact, **strong human**. Conditional form `attest:<fact> when task:<flag>` is required unless the task's flag is explicitly false (fail-safe: unknown still gates) |
| `verify:<check>` | a fresh `dais check` record for the task's PR, else the machine's declared `checks.<check>` command passing, else the firing role's explicit `--verify` self-assertion; fails closed |

**Yolo mode** is the gates' dial turned to zero, deliberately: `dais yolo <project> on
[--for 48h] [--veto 30m]` makes the dispatcher auto-fire the founder gates the machine
explicitly tags `"yolo": true` (the stock coding machine tags `approve`, so yolo means
full-speed inward: proposals flow, QA still verifies, releases still park). The honesty
rule: yolo auto-satisfies `confirm` and nothing else; it never forges a `typed_confirm`
or an `attest:` (those record human testimony, and the release greenlight stays yours
even under yolo). The board shows a loud YOLO badge, every auto-fired gate leaves an
attributed audit note on the task, and `--for` self-expires the mode. Design:
[`design/yolo-mode.md`](design/yolo-mode.md).

**Effects** keep composition inside the machine: an edge can `spawn` a task (QA-fail spawns
the fix), `aggregate` a set (a release pulls in every `approved` task), or `then`-fire edges
on what it encompasses (shipping a release closes its children); each nested change still a
real, guarded edge. `fire` is atomic: a transition and all its effects commit or roll back
together. A `spawn` may also be a LIST, so one approval fans out an initiative; an entry's
`after: <template>` chains it behind the sibling spawned earlier in the same fan-out
(`blocked_on`), and the scheduler runs the chain in order:

```json
"effect": {"spawn": [{"template": "design", "initial": "ready", "by": "engineer"},
                     {"template": "build",  "initial": "ready", "by": "engineer", "after": "design"},
                     {"template": "docs",   "initial": "ready", "by": "writer",   "after": "build"}]}
```

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
`l` log pager · `L` live log wall (`/` filters agents, `e` error lines only) · `m` the
project's machine as a table (states, band, count, acting role, edges, task ids) · `space`
marks a task and `u` clears the marks: a keyed edge action or `+`/`-` then applies to every
marked task, each through the same guards, and the flash sums the batch up · `q` quits (with
confirm) · `esc` backs out one level.

**No wasted-run loops.** A role that keeps running without moving anything is throttled
(45 minutes), then stalled until its world changes. "Moving anything" is a net status diff:
the role's dispatch set as it read when the run launched versus now, after the tick's
reconcile, so a claim that an interrupt reverted counts as nothing
([design/probe-loop-cooldown.md](design/probe-loop-cooldown.md)).

**Why is nothing running?** Every tick that launches nothing journals why
(`projects/.watch.log`: a throttled role, a stalled one, a cooling provider, an idle-check
skip, a spent budget, pause). The board reads it back: the ALL row's inspector shows each idle
project's last reason with its age and when it retries (`idle · throttle acme/lead — … (12m
ago, retry ≈33m)`), the vitals strip shows the newest one in a few words when nothing runs,
and `dais status` prints `⏱ last tick: …` under each idle project.

**What the inspector adds** (first top batch of the plan): the reviewing role's verdict and
the recorded checks on the selected task; the PR's size, state, and mergeability via `gh`
(cached two minutes; it says so when `gh` is missing); a long notes log folds to its last six
entries; each run in the history and in the FEED shows the tokens and dollars it cost; `/`
searches notes as well as titles; the vitals strip shows what the next tick would launch when
the loop is idle, and warns `⚠ COLLISION <project>` when two live runs share one working
tree without worktree isolation.

**Be told when work waits on you.** Put a `notify:` command in `dais.yaml`; it receives one
message on stdin. The loop sends once per arrival: a task newly parked in NEEDS YOU (a bounce
that comes back is a new arrival), a task newly held over budget, a spent daily budget once a
day. Try it with `dais notify test "hello"`.

```yaml
notify: curl -s -X POST "https://api.telegram.org/bot$TG_TOKEN/sendMessage" -d "chat_id=$TG_CHAT" --data-urlencode text@-
# macOS:  notify: m="$(cat)"; osascript -e "display notification \"$m\" with title \"dais\""
# a log:  notify: cat >> "$DAIS_HOME/notify.log"
```

**Manual vs. the loop.** `dais watch` is the continuous auto-dispatcher. `dais start <id>`,
`R`, and `t` are on-demand runs that fire one agent now and bypass pause. `start` runs the
role the machine dispatches for the task's state, honoring the dependency chain.

## The web page (`dais web`)

The same board in your browser, over the same data layer as `dais top`: `dais web [port]`
prints a localhost URL with a per-launch token (a wrong token is a 404 everywhere). Tabs:
the **board** by band per project with the selected task's brief and its fireable edges;
the **machine** as a live diagram (counts on each state, founder edges marked); **charts**:
stat tiles (tokens, cost and runs today, what waits on you), prompt tokens per day stacked
by role with a table view, the last 24 hours of runs as a timeline colored by outcome, and
each founder gate's approved-unchanged share with the yolo candidates marked; **runs** with
tokens and cost; **cost** and **retro**. Actions go through `dais fire` in a subprocess, so
the engine enforces every guard exactly as the CLI does: the page asks for what
`prompts_for` says the edge needs (typing the task id, naming the attest, a note, a click)
and can never fire a gate the CLI would refuse. Notes, priority, and a budget lift go through
`dais task set`; pause and resume through the CLI. Light and dark follow the system, with a
selector that persists per browser. Phone: `tailscale serve <port>`; the token keeps a shared
network from being enough. Stdlib only, one HTML file, no build step.

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
you declare `concurrency: 2..5` on may STACK: the dispatcher launches another run of the
*already-live role* when it has slot headroom and there are more dispatchable tasks than live
runs. It never launches a *second role* into a busy repo, and it never stacks onto a single
task: each run pins its task to `runs.task_id` at startup, and dispatch skips tasks a live run
already holds — so the stacked run takes the *next* task down the queue. Turn it up only where
runs are truly independent per task (content drafting, review-only roles). One caution: roles
whose agents run a shared test stack (a scratch DB) will collide; isolate that first (see
`isolation: worktree`). Cadence roles (`every:Nh`) should stay at 1 (they groom shared state);
`dais lint` warns.

**Let Claude design a role:** `dais role new <project> --desc "what it does"` proposes a
persona + config from your project's existing roles; you confirm.

## Providers: anthropic + openai (and packs)

Each agent runs against a **provider pack**, chosen per-role. A pack is a directory under
`harness/providers/<name>/`: `run.sh` defines `provider_run` (the adapter: exec the CLI, pipe its
JSONL through `fmt-stream.py --provider <name>`), `stream.py` maps the CLI's events onto the
shared log markers and the ledger, `caps.txt` lists the usage-limit patterns, and `pack.json`
names the CLI, the API key variable, and the default model. The preflight, lint, doctor, the
cap detector, and the formatter all discover packs from the directory, so a third provider is
a folder, not a code change. Two ship:

```
---
model: gpt-5.1-codex-mini
provider: openai        # anthropic (default, claude CLI) | openai (codex exec)
auth: api                # subscription (default, CLI login) | api (metered)
---
```

A mixed cast is the normal case: put `provider: openai` in ONE role's frontmatter and the rest
of the project stays on claude. `dais project <name>` shows each role's provider next to its
model; `dais top`'s inspector reads `runs as qa · openai · gpt-5.4`; `dais role new` asks the
designer which provider a new role runs on. `dais lint` warns when a role's provider CLI is not
on PATH, and a run refuses to start (recording nothing) until it is. A codex turn that dies on an
API error, such as a model id your ChatGPT plan can't use, is recorded as a `failed` run, not a
silent success. Codex runs are `--ephemeral` (no session piles up in `~/.codex` per tick), but
they DO read your `~/.codex/config.toml`: a `notify` hook there fires on every headless run,
and its `model = "…"` line is the default for an openai role with no `model:` of its own (shown
in `dais project`, passed explicitly, recorded on the run).

Resolution (frontmatter → legacy roles file → `project.yaml` → defaults) is one authority,
`router.agent_setup`, read by every consumer (the scheduler, `run-agent.sh`, `dais project`).
**Model keys are provider-scoped:** a project-wide `model:` in `project.yaml` only applies to
roles resolved to that project's *default* provider; it never leaks a `claude-opus-4-8` id
onto a role you've overridden to `provider: openai` (or vice versa). Give a per-role override
its own `model:` in that role's frontmatter instead.

**A third pack ships: `provider: opencode`.** [opencode](https://opencode.ai) is one adapter
for 75+ providers including local models; name the model as `provider/model`
(`model: anthropic/claude-sonnet-5`, `openrouter/…`, `ollama/…`). opencode holds the provider
keys itself (`opencode auth`), review and draft roles run its read-only `plan` agent, edit roles
its default build agent, and headless runs always skip permission prompts (a prompt would hang
the run). `effort:` maps to `--variant`. Its tokens and session id land in the ledger.

**Any OpenAI-compatible endpoint, through the openai pack.** Codex talks to whatever
`model_providers` entry you name; dais writes it from the role's frontmatter (or project-wide):

```
---
provider: openai
model: deepseek-chat
model_provider: deepseek                  # any name; codex's config key
base_url: https://api.deepseek.com/v1     # OpenRouter, Groq, DeepSeek, vLLM, …
env_key: DEEPSEEK_API_KEY                 # the env var codex reads the key from
---
```

`local: ollama` (with `model: llama3`) runs a local model instead. A claude role takes
`base_url:` too, exported as `ANTHROPIC_BASE_URL` for a gateway such as LiteLLM.

**Fallback across providers.** `fallback_model:` may live on another pack: add
`fallback_provider: openai` and the run that hits the Claude usage limit finishes on codex
(recorded as such on the run row, the ledger, and the per-provider cap gate). Both CLIs are
preflighted; a cross-provider attempt never resumes a session (a session belongs to one CLI).

**Accounts: more than one login per provider.** Two 5x Max plans beat one 20x for
throughput, and a cap on one says nothing about the other. Name them once, user-level, in
`~/.dais/accounts.yaml` (credentials never live in a workspace):

```yaml
accounts:
  max-a:   {provider: anthropic, kind: subscription, config_dir: ~/.dais/accounts/max-a}
  max-b:   {provider: anthropic, kind: subscription, config_dir: ~/.dais/accounts/max-b, window: 5h}
  api-1:   {provider: anthropic, kind: api, key_env: ANTHROPIC_API_KEY_1}
  chatgpt: {provider: openai,    kind: subscription, config_dir: ~/.dais/accounts/chatgpt}
pools:
  max: {members: [max-a, max-b], policy: least-recently-capped}   # or round-robin | first-free
```

Log each subscription in once by hand: `dais account login max-b` runs the CLI's own login
with that account's config directory (`CLAUDE_CONFIG_DIR` for claude, `CODEX_HOME` for codex;
the pack declares which). A role names an account or a pool the same way it names a model:
`account: max-a` / `account: pool:max` in its frontmatter (or `account_<role>` / `account` in
`project.yaml`); `fallback_account: chatgpt` with `fallback_model: gpt-5.4` is the next tier.
The account decides the provider and the auth kind, so `provider:`/`auth:` become the shortcut
for each provider's implicit account (the ambient login), and a workspace without the file
changes nothing.

A pool may span providers only when the foreign member carries its own model, since model ids
differ per provider: `members: [max-a, chatgpt:gpt-5.4]` (lint rejects a mixed pool without one).
Use that only for a role that tolerates either model; the default pattern is a same-provider
pool with a cross-provider fallback tier.

A run tries the pool's free members first (same provider first, then the fallback tier), and a
capped attempt marks that ACCOUNT for its window (`~/.dais/accounts/<name>.cooldown`; a later
success clears it). The dispatcher's cooling gate keys on the account too: a role is withheld
only when every account in its plan is cooling, and `dais status`/top/web name the cooling
accounts. `dais account list` shows each account's cap state, `dais account clear <name>` drops
a marker, `dais doctor` checks every used account's login (or key), and `dais cost --by
account` splits the ledger by credential. `dais lint` rejects an unknown account, a pool
naming one, or an account that contradicts an explicit `provider:`.

**`auth: api`** reads the provider's standard key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) from
the process environment, then `~/.dais/env`, then `$DAIS_HOME/.env` (workspace override); first
one set wins. Never put a key in `project.yaml` or a persona file (`dais lint` warns on
secret-shaped values); `dais init` gitignores `.env` for you. `auth: subscription` (the default)
runs the CLI as already logged in, nothing to configure.

**Access is enforced, not just asked.** A claude role that is not `edit` runs with
`Edit`/`Write` disallowed AND a PreToolUse hook (`harness/hooks/guard.sh`) that refuses outward
shell with a message the model reads: `git push|commit|merge|rebase|reset`, `gh pr
merge|create|close`, `rm -rf`. The guard classifies the parsed command, not the raw text: it
peels `env`/`sudo`/`xargs`/`timeout` wrappers and absolute paths, looks inside `bash -c "…"`,
`eval` and inline `python3 -c`/`node -e` code, and matches git, gh and rm on their subcommand
and flags (`rm -fr`, `git -C x push`, `git branch -D`, `gh api -X POST` are caught; `grep push`,
`rm -f tmp`, `echo 'git push' > notes` pass). It is a guardrail, not a sandbox: a script file
the role runs is not opened. Review and draft roles read and test; only an edit role commits,
pushes, merges, or deletes. Codex has no hooks; its sandbox is the guard there. opencode roles
below `edit` run its read-only `plan` agent (the same posture: an unattended run cannot answer
a permission prompt, so both edit paths skip prompts by design — see the codex note below).

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

## The prompt carries the context; the agent starts on the work

Every run used to open with the same three round trips: `dais task show` on its task, then a
Read of the workspace CONTEXT.md, then a Read of the project's. The harness already had all
three in hand, so it now inlines them into the prompt (the cached prefix): the pinned task's
full record as of launch, and both CONTEXT files, each capped at 24KB with a loud truncation
marker (`dais lint` and `dais doctor` warn on an oversized one). Agents still run
`dais task show` for any OTHER task they touch.

**A multi-run task resumes its session.** When the same role is dispatched again on the same
task within six hours and its last run on it succeeded, the claude adapter passes `--resume`
with that run's session id and a short continuation prompt (the session already holds the
rules, the CONTEXT files, and the persona). A failed run is never resumed from, and a session
claude can no longer find fails that one run and the next starts fresh. `resume: off` in a
role's frontmatter or `project.yaml` opts out. Codex roles always start fresh.

## The agent profile: what a run inherits from your Claude Code

A `claude -p` run inherits your WHOLE Claude Code install by default: every plugin's skills,
every MCP server (including claude.ai connectors such as mail, calendar, and payments), your
hooks, and your personal CLAUDE.md. Measured on a real install that was about 10K tokens on
every turn before dais's own prompt, and it put a mail client in every engineer's tool list
with permissions bypassed. So roles run **`context: lean`** by default: the run keeps the
REPO's own settings (`--setting-sources project,local`: its CLAUDE.md and `.claude/settings*.json`
survive) and gets back exactly what the role allowlists:

```
---
mcp: qmd, gbrain          # user-level MCP servers from ~/.claude.json
plugins: supabase          # installed plugins, from ~/.claude/plugins/cache
---
```

Both keys also work project-wide in `project.yaml`. A name that resolves to nothing is said
at run start and the run proceeds without it. `context: full` restores the historical
invocation for a role that needs something the allowlists can't express (skills installed
under `~/.claude/skills` have no per-run loader); `dais lint` warns on every such role. Codex
roles are unaffected (codex reads its own `~/.codex/config.toml`). Logs now name the skill on
every `Skill` call, so a week of runs tells you which roles need which `plugins:`.

## The idle check: a cadence role does not run on an unchanged board

A cadence role (`trigger: every:Nh`) used to run on its clock no matter what, and most of
those runs found nothing to do: measured across one workspace, 75–87% of lead runs were
no-ops, each paying the full startup because the "is anything new?" check happened inside the
model. Now the harness answers it. After a successful cadence run the role records a
fingerprint of the board as it left it (every task's id, state, and priority: not notes, not
timestamps, so a role cannot wake itself by writing notes). On the next interval the router
skips the role while the board still matches, and journals why (`idle-check: skipping
winterbraid/lead — board unchanged since its last run 5.2h ago`). A new task, a state change,
or a priority change wakes it; a 24-hour heartbeat runs it regardless. Reactive dispatch is
untouched: a `proposed` task still wakes the lead at once.

## Structured verdicts, quiet hours, throughput

**Verdicts.** A reviewing role hands its verdict over WITH the transition: `dais fire <id> pass
--verdict '{"verdict":"pass","summary":"…","checks":["bun test"],"risks":[]}'`. The JSON is
stored on the task (plus who, which verb, when) and a rendered line rides the notes log; the
stock QA persona sends one on `pass` and `fail`. Provider-agnostic on purpose: it is a CLI
convention, not a model feature. `dais brief` and the change-request analytics read it.

**Quiet hours.** `quiet_hours: 23-7` (local time) in `dais.yaml` or a project's `project.yaml`:
cadence roles (`every:Nh`) sleep through the window; reactive work never does.

**Throughput.** `parallel: 3` in `dais.yaml` is the loop's default width (`dais watch` with no
explicit N, and every launchd tick, read it). `worktree_link: node_modules, .venv` in
`project.yaml` symlinks the repo's installed dependency dirs into each fresh isolated worktree
before `worktree_setup` runs, so a QA run stops paying a full install per run.

## Tiers: the top model only where the board says it matters

`model_by_priority` and `effort_by_priority`, in a role's frontmatter or project-wide in
`project.yaml`, override the role's model and effort for a run by the pinned task's priority:

```
model_by_priority: critical=claude-fable-5, low=claude-haiku-4-5
effort_by_priority: critical=high, low=low
```

A priority with no tier keeps the role's own setting; the run row records the model actually
used, so `dais cost --by role` shows the split.

## Budget caps: no run is unbounded unless you say so

Three per-role (or project-wide) caps, all unset by default, which is the historical
unbounded behavior:

```
---
max_minutes: 30        # wall-clock: the harness kills the run's whole process tree (both providers)
max_turns: 60          # claude only  (--max-turns)
max_budget_usd: 5      # claude only  (--max-budget-usd; API-equivalent dollars, also on a subscription)
---
```

A run that hits any cap is recorded `failed` with the cap named in its log (`⏱ timed out after
30 min`, `✗ stopped: max turns reached`), so the per-provider error backoff still protects you
from a role that hits its cap every tick; the task stays where it was. Codex has no turn or
dollar flag, so only `max_minutes` binds a codex run; `dais lint` says so if you set the others
on an openai role. Read `dais cost --by role` before choosing numbers.

## Spend limits: a ceiling per task, a budget per day

Both read the run ledger (below), both unset by default.

**Task spend ceiling** (`project.yaml`): `task_max_runs: 6` and/or `task_max_tokens: 2M`. A
task's spend is the distinct runs that touched it and their prompt tokens. Past either ceiling
the dispatcher withholds the task, exactly like a dependency-blocked one, and the board says so:
the row reads `⛔ [7 runs·2.30M] title`, the inspector and `dais status` name the task, and the
vitals strip counts `⛔ N OVER BUDGET` apart from the gate count (a spend hold is not a machine
gate). You decide: `dais task set <id> --budget-lift` resets the meter (only runs after the
stamp count), or cancel it. The 14-run probe loop of 2026-07-18 would have stopped at run 3.

**Daily loop budget**: `daily_budget: 2M` in `dais.yaml` (the workspace), overridable for one
loop with `dais watch --budget 500k`, or per project in `project.yaml`. Over it, the loop
launches nothing until tomorrow (UTC): the tick journals why, `dais status` and the vitals
strip show `BUDGET SPENT 2.1M/2M tokens`. A `$20` budget counts claude-reported dollars only, so
codex runs count nothing against it; tokens are the honest unit for a mixed workspace.

## The run ledger: what each run cost

Every run records what it consumed (migration 0008: `dais migrate` with the loop paused): the
whole prompt in tokens, the cached share, output tokens, turns, and, for claude runs, the
dollar figure Claude Code itself reports (`total_cost_usd`: the API-equivalent, even on a
subscription). Codex reports tokens but no dollars, so codex rows show tokens only. **Tokens
are the unit to compare across providers.** `dais cost` reports per project, role, or task
(a task's cost is the sum of the runs that touched it), with each role's no-op share: runs
that succeeded and fired nothing. Runs from before the migration have no usage: the logs did
not keep it.

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
| `dais brief <id>` | the decision packet for a gate on one screen: how long it has waited, what a release encompasses (each PR with size and mergeability via `gh`, the QA verdict, recorded checks, last note), the migrations flag the greenlight demands, spend so far, a suspected duplicate, an escalated task's bounce history, and exactly what you can fire with its guards and effects |
| `dais check <id> [<check>] [--branch B]` | run the machine's declared check in a throwaway worktree of the task's PR branch and record the result; `verify:<check>` honors it for 24h (zero tokens, real attestation) |
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
| `dais learn <project> "…"` | a durable decision/gotcha for the project's CONTEXT.md — an agent's learn lands in a review queue (`--review`, `--accept N\|all`, `--drop N`); the founder's own writes CONTEXT.md directly |
| `dais doctor` | preflight: provider CLIs + logins, every used account's login or key, API keys for `auth: api` roles, pending migrations, each project's repo, CONTEXT sizes, dispatcher markers; exit 1 on a blocker |
| `dais account list\|login <name>\|clear <name>` | the founder's accounts (`~/.dais/accounts.yaml`): each account's cap state and the pools; log a subscription account in under its own config dir; drop a cap marker |
| `dais logs <project> [N]` | recent runs + their saved log paths (+ tokens · cost per run) |
| `dais retro [--since 30d]` | your loop, measured from the transition log: per gate, how many decisions, how often approved unchanged, the median wait on you; QA pass/fail per project; bounced tasks; what shipped; and the yolo candidates (≥90% approved unchanged over ≥10 decisions) |
| `dais cost [project] [--since 7d] [--by project\|role\|task\|account]` | the run ledger: tokens per project, role, task, or account, dollars where the provider reported them, no-op share |
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
