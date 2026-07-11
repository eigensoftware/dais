# Authored task state machines

Status: implemented (`harness/machine.py` + `harness/machines/`). Superseded the
fixed status set + the `ship`/`deploy`/`automerge` special cases.

## The model — one atom, two graphs, one bridge

- **Atom: the task.** The only unit of work. Everything — an epic, a proposal, a
  bug fix, a release — is a task.
- **Graph 1 — lifecycle.** A founder-**authored** state machine, run per task.
  Not a fixed status set, not a "craft" template you pick: the founder draws the
  graph. States are lifecycle positions; **roles** are the actors.
- **Graph 2 — composition.** Task→task edges: `spawned_by` (provenance),
  `encompasses`/`part_of` (aggregation), `blocked_on` (dependency). Tasks
  compose recursively — a task can spawn many and encompass many.
- **Bridge: effects.** Composition edges are produced by transition *effects*.
  "spawn N tasks" and "aggregate" are effect types alongside "run a script" — no
  new primitive.

The load-bearing invariant that keeps effects from being an escape hatch:
**every state change is a transition. Effects *fire edges*; they never poke
state.** A release closing eight children does it by firing eight real
`→ done` edges, each still subject to that edge's guards. So even spawned and
aggregated work stays inside the machine and stays lint-able.

## Roles

A role is the cast, not the choreography — the state→role routing that used to
live in the `roles` file's `handles`/`trigger` columns now lives on the machine's
edges (`by`). A role shrinks to:

```yaml
roles:
  founder:  { human: true }        # the top approver; parks instead of dispatching
  lead:     { access: draft }
  engineer: { access: edit }
  qa:       { access: review }
```

`founder` and `system` are implicit actors: `founder` = a human decision (the
task parks until you act); `system` = an automatic edge (interrupt rewind, an
unblocked dependency, a release effect firing a child's edge).

The machine's `roles` values are authoritative for `access` (run-agent enforces from here);
scheduling/cadence — model, effort, provider, auth, trigger, prec, playbook — lives in each
agent's own frontmatter (`agents/<role>.md`), resolved by `router.agent_setup` (see the
README's [Playbooks](../README.md#playbooks-running-any-craft) and
[Providers](../README.md#providers-anthropic--openai) sections).

## Schema

```yaml
states:
  <name>: { initial: true?, terminal: true?, pool: true?, band: <BAND>? }   # tags only
edges:
  - from: <state>
    to:   <state>
    by:   <role|founder|system>       # exactly one actor
    verb: <label>                     # human name for the edge
    guards: [ ... ]                   # preconditions (see vocab); default none
    effect: { spawn|aggregate|script|then }   # optional side effect
checks:                                # optional: verify:<name> -> a real command
  <name>: <shell command>              # exit 0 = the check passed
```

`fire` is ATOMIC: a transition and all its effects (spawns, links, nested
`then` fires) commit together or roll back together, and the state write is a
compare-and-swap — two racing fires can't both apply.

System edge VERBS are harness event names — authoring one is how a machine
opts into that harness behavior: `interrupt` (fired by the dispatcher's
orphan-reconcile when no agent is live) and any edge guarded `unblocked`
(fired each dispatch tick once its blockers are terminal).

Dispatch is derived: the scheduler launches the (single) agent role with an
outgoing edge from a task's current state. `founder`/`system` edges are not
dispatched. `pool: true` opts a state into any-of-N dispatch (multiple agent
roles allowed); the pick is deterministic — the first pool member in the
machine's `roles` declaration order (declaration order = precedence).

### Guard vocabulary (closed — compose, don't invent)

| Guard | Satisfied by | Note |
|---|---|---|
| `confirm` | a click | weak; an auto-approver can satisfy it |
| `typed_confirm` | a human typing a phrase | **strong human** |
| `attest:<fact>` | a human asserting an unverifiable fact | **strong human**; optional conditional `attest:<fact> when task:<flag>` — required unless the task's `<flag>` column is explicitly false (NULL/unknown still requires it) |
| `verify:<check>` | an automatic check (tests, CI, no-conflict, brand_voice) | the machine's `checks.<check>` command runs on fire; absent that, only an explicit `--verify` self-assertion by the firing role passes it (fails closed) |
| `role:<r>` | actor authorization | |
| `note` | `--notes "..."` on the fire | the edge CARRIES information (an answer, a change request): the note is appended to the task's log atomically with the transition, before effects — so a spawn's notes-inheritance carries it. `--notes` also rides any unguarded edge optionally |

Strong-human guards (`typed_confirm`, `attest:`) are what make an outward edge
*structurally un-automatable* — no auto-approver can forge them. That is the
protection mechanism; danger is declared per-edge, not coded per-action.

### Effects

- `spawn: { template, initial, by, rel }` — create task(s). `rel ∈
  {from_proposal, blocks_parent, part_of}`.
- `aggregate: { select }` — pull matching tasks into this task's `encompasses`.
- `script: { name, outward: bool }` — DECLARATIVE metadata only: it marks that this
  edge's actor performs an external action (merge/deploy/publish), and `outward` is
  what lint W3 reads to demand a human gate on the edge. The engine does NOT execute
  anything (machine.py `_apply_effect` deliberately ignores it) — the named actor
  does the action per the repo's own docs, then fires the edge.
- `then: "encompassed:<state>-><state>"` — fire an edge on related tasks.

Edges may also carry `"yolo": true`: an authored opt-in marking this edge as the
default action under yolo mode (`dais yolo <project> on`), where the dispatcher
auto-fires it as the human actor. Only permission guards survive automation:
yolo auto-satisfies `confirm` and nothing else, so a yolo tag on an edge with
`typed_confirm`/`attest:`/`verify:` is a lint error (E6), and a yolo tag on an
outward edge draws a warning (W4). Full design: `yolo-mode.md`.

## Lint — coherence only, never policy

The structure imposes **no** inherent limits. Lint blocks only on incoherence;
everything policy/safety-flavored is a warning you can wave off. (Implemented as
`lint()` in `harness/machine.py`; run it with `dais lint`.)

**Errors (block):** E1 referential integrity · E2 no dead-end (every
non-terminal has an out-edge) · E3 unambiguous dispatch · E4 has an initial and
a terminal (and a valid `entry`) · E5 no duplicate (from, verb) edge.

**Warnings (advisory):** W1 unreachable-from-initial · W2 can't-reach-terminal ·
W3 outward effect with no strong-human guard on it or its approach.

Green errors ⇒ build whatever shape you want.

## The coding default (authored, fully editable)

`harness/machines/coding.machine.json` is the runnable form (this YAML is the
original design sketch — state names evolved: awaiting_release→approved,
release_ready→release_open, backlog folded into ready).
It exercises every feature: proposal→spawn, qa-fail→spawn-fix + block, batched
release via aggregate, upstream human gate on deploy, and a rollback path.

```yaml
states:
  proposed:         { initial: true }     # lead fleshes the idea to a spec
  proposal_review:  {}                     # awaits founder
  backlog:          {}
  ready:            {}                      # engineer implements
  doing:            {}                      # in-flight (lock)
  qa_review:        {}                      # qa decides
  blocked:          {}                      # parent waits on a spawned fix
  deferred:         {}
  awaiting_release: {}                      # done code, parked for a release
  release_ready:    { initial: true }       # entry point for a release task
  release_review:   {}
  releasing:        {}
  release_failed:   {}
  done:             { terminal: true }
  cancelled:        { terminal: true }

edges:
  # intake / proposal — lead fleshes; founder approves; approval SPAWNS impl tasks
  - { from: proposed,        to: proposal_review, by: lead,     verb: submit }
  - { from: proposal_review, to: backlog,         by: founder,  verb: approve,
      guards: [confirm, "verify:def_of_ready"],
      effect: { spawn: { template: impl, initial: ready, by: engineer, rel: from_proposal } } }
  - { from: proposal_review, to: proposed,        by: founder,  verb: request_changes }   # feedback loop
  - { from: proposed,        to: cancelled,       by: founder,  verb: reject, guards: [confirm] }

  # implementation
  - { from: backlog, to: ready,     by: founder,  verb: promote }
  - { from: ready,   to: doing,     by: engineer, verb: claim }
  - { from: doing,   to: qa_review, by: engineer, verb: complete }
  - { from: doing,   to: ready,     by: system,   verb: interrupt }

  # QA — a FAIL spawns a fix task back to the engineer and blocks the parent
  - { from: qa_review, to: awaiting_release, by: qa, verb: pass, guards: ["verify:tests_pass"] }
  - { from: qa_review, to: blocked,          by: qa, verb: fail,
      effect: { spawn: { template: fix, initial: ready, by: engineer, rel: blocks_parent } } }
  - { from: blocked,   to: qa_review,         by: system, verb: unblocked, guards: [unblocked] }

  # release — a SEPARATE task that batches (encompasses) the awaiting_release set
  - { from: awaiting_release, to: done, by: system, verb: released }   # fired by a release task's effect
  - { from: release_ready,  to: release_review, by: engineer, verb: assemble,
      effect: { aggregate: { select: "state=awaiting_release" } } }
  - { from: release_review, to: releasing, by: founder, verb: greenlight,      # the human gate
      guards: ["typed_confirm", "attest:migrations_applied when task:touches_migrations"] }
  - { from: release_review, to: cancelled, by: founder, verb: abort, guards: [confirm] }
  - { from: releasing, to: done, by: engineer, verb: shipped,
      effect: { script: { name: release, outward: true }, then: "encompassed:awaiting_release->done" } }
  - { from: releasing, to: release_failed, by: system, verb: release_error,     # rollback path
      effect: { spawn: { template: rollback, initial: ready, by: engineer, rel: blocks_parent } } }
  - { from: release_failed, to: release_ready, by: founder, verb: retry }
  - { from: release_failed, to: cancelled,     by: founder, verb: give_up, guards: [confirm] }
```

### How the earlier open questions resolve inside this — no enumeration needed

- **Merge ≠ deploy / graduated friction:** the deploy gate is one edge
  (`release_review→releasing`) carrying `typed_confirm` + conditional `attest`;
  a lower-risk action carries `confirm` or nothing. Friction is per-edge data.
- **Batched release:** a release task `aggregate`s the `awaiting_release` set and
  its `then` fires each child's `released` edge — so it encompasses many.
- **QA spawns a fix task:** the `fail` edge spawns a fix task (`blocks_parent`);
  the parent parks in `blocked` and returns to `qa_review` when unblocked.
- **Rollback (the release-failure gap):** `releasing→release_failed` spawns a
  rollback task; lint W2 would have flagged the strand if we'd forgotten the
  exit edges out of `release_failed`.

## What this replaces in today's code

- Statuses `needs_qa` / `needs_review` / `ready_to_merge` → ordinary review
  states with an approver (`by`).
- `dais ship` verb → the `releasing` edge's `script` effect + its guards.
- `deploy` agent + `deploy_state` + `.deploy-rev` + deploy TUI band → release-
  task edges.
- `automerge` "no founder click" → a configurable `auto:` actor, still unable to
  fire any edge carrying a strong-human guard.
- The `roles` file's `handles`/`trigger` columns → edge `by`. The scheduler and
  `actions.py` read the machine; no hardcoded status knowledge anywhere.
