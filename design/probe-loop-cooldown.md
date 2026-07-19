# Probe-loop cooldown

Status: **DESIGN ONLY — not implemented.** Written for founder review per the
2026-07-18 paper-cuts batch (item F). No code in this doc's commit.

## The incident

Overnight 2026-07-18, one task was dispatched to its engineer **14 times** under
`dais watch`, back to back, with neither the no-progress throttle nor the
stall-escalation marker (`design` companion: `fa459de`, see `harness/dispatch.sh`)
ever kicking in.

The shape of each run:

1. The engineer is dispatched for a `ready` task and fires `claim` (`ready -> doing`,
   `run_tasks` verb `claim`) — a real, non-`touch` verb.
2. The run then aborts on an **environmental precondition** it can't satisfy (e.g. a
   locked display a UI-driving playbook needs) — not a problem with the task itself,
   a problem with the machine the agent is running on right now.
3. Nothing fires `complete`. The run ends without a further transition.
4. On the next tick, `dispatch.sh`'s reconcile step finds no live lock, marks the
   orphaned `running` row `interrupted`, and fires the machine's system `interrupt`
   edge (`doing -> ready`, present in the stock coding template) — the task is back at
   `ready`, indistinguishable from where it started.
5. The next tick dispatches the engineer again. Go to 1.

Fourteen iterations of that loop, one run each, all night, for zero net progress.

## Why the existing defenses miss it

Both of `dispatch.sh`'s existing guards key off the same signal: **does
`run_tasks` have a row for this run with `verb != 'touch'`?**

- The **45-minute no-progress throttle** (`last = "succeeded|1|0"` check) treats any
  non-`touch` verb as "did something" and skips the cooldown.
- The **stall-escalation streak** (`fa459de`) counts a run as a "no-op" toward its
  2-in-a-row threshold using the exact same `verb != 'touch'` count.

`claim` is a real, meaningful verb — record-for-record it is *supposed* to mean
progress, and normally does. The loop above defeats both guards for the same reason:
**the verb was fired, but a system edge silently undid it one tick later.**
`run_tasks` has no notion of "and then it was reverted" — it is an append-only trail
of verbs fired, not a ledger of net state. From the throttle/stall's point of view,
run 14 looks exactly as productive as run 1.

This is the mirror image of item E in the same batch (a verify-gated `releasing`
task that stalls despite *legitimate* per-poll progress, because touch-only notes
don't count as progress). Item E is a false positive on the "no progress" signal;
this incident is a false negative — `verb != 'touch'` is being asked to stand in for
"real progress" in both directions and isn't equal to the task either way.

## Candidate designs

### A — same-task claim→revert counter

Track, per `(project, role, task)`, how many consecutive times a role has claimed
that specific task and the task was reverted (system `interrupt`, or more generally:
ended a run cycle back at or before the state it started in) without ever reaching a
state strictly further along the graph. After *N* such round-trips, treat it like a
stall — same `.stalled-<role>` marker, or a sibling `.looping-<role>-<task>` marker
scoped to just that task.

**Pro:** narrowly and precisely targets this exact signature (claim, abort, revert,
repeat) with an easy-to-explain trigger condition.
**Con:** new per-(role,task) bookkeeping (today's stall marker is per-role only); and
it only catches *this* shape. A role that rotates across several different tasks,
claiming and reverting a different one each tick, nets to the identical "zero
progress forever" outcome but never repeats the same task id twice in a row — this
design would never see it.

### B — an agent-declarable `blocked-env` signal with a retry-after

Give agents a real, honest way to say "I cannot proceed — not because this task is
wrong, but because *this run's environment* can't do the work right now" — a new
system-owned verb (e.g. `doing -> blocked_env` on a state the machine can declare) or
a reserved note-verb the CLI understands, carrying a human-readable reason and a
retry-after duration. The dispatcher parks that role (or that task) until the
retry-after elapses, then tries again — no permanent stall, no silent revert-and-loop,
and `dais top`/`dais status` can show *why*, verbatim ("display locked, retry in
30m") instead of an opaque STALLED badge.

**Pro:** the most semantically honest option — the founder sees the actual reason,
not an inference; and it composes cleanly with the existing guard vocabulary
(`design/machine-model.md`'s guards are already a small, principled set).
**Con:** requires agent buy-in. An agent that doesn't know to declare it (or hits an
environmental failure its playbook never anticipated) still silently loops — this
can never be the *only* defense, only a richer layer on top of one that doesn't
require the agent's cooperation.

### C — redefine "progress" for the stall/throttle check as a net status diff

Instead of asking "did `run_tasks` record a non-`touch` verb," ask "is the task's
status, checked *after* the tick's reconcile/interrupt sweep has run, anywhere
different from where it was before this streak of runs started?" A `claim` that gets
reverted by `interrupt` nets to the same status it started at — correctly counts as
no progress. A `claim` that sticks (because the run actually got somewhere) nets to
a real status change — correctly counts as progress, same as today.

**Pro:** smallest change — same mechanism, better signal; no new agent-facing
vocabulary, no new marker shape; catches *any* loop shape (claim/revert, or rotating
across tasks, or anything else) because it looks at the *outcome*, not the verb
name. Directly symmetric with item E's fix in this same batch (both corrected the
same flawed assumption — that a `run_tasks` verb-count is a reliable progress proxy
— in opposite directions), so the two land as one coherent model instead of two
special cases.
**Con:** needs the diff to be evaluated at the right moment — after reconcile has had
a chance to fire `interrupt`, not before, or a claim-then-abort run would look like
progress for one extra tick. Slightly more bookkeeping than today (need "status at
streak start," not just "did the last run touch something"), though it reuses the
existing per-role run history query, just widened to also read `tasks.status` over
that window.

## Tradeoffs, side by side

| | catches claim/revert | catches rotating-task loops | needs agent buy-in | new vocabulary | founder sees WHY |
|---|---|---|---|---|---|
| A — claim→revert counter | yes | no | no | new marker shape | no (generic STALL) |
| B — `blocked-env` signal | only if declared | only if declared | **yes** | new verb/marker | **yes** |
| C — status-diff progress | yes | yes | no | none | no (generic STALL) |

## Recommendation

Ship **C** first — it is the general, defensive backstop: no agent cooperation
required, no new machine vocabulary, and it closes the gap for this incident's exact
shape *and* the rotating-task variant A can't see, by fixing the same flawed
assumption item E's fix in this batch already had to correct. It should be written
as one coherent change to the existing throttle/stall logic, not bolted on beside it.

Treat **B** as a valuable, separate follow-up: even with C in place, "STALLED" is an
opaque signal — a founder opening `dais top` at 8am still has to go dig through logs
to learn it was a locked display. An agent that *can* diagnose its own environmental
blocker should be able to say so plainly, with a retry-after, once the playbooks are
updated to detect and declare the common cases (locked display, missing display,
offline). B is additive on top of C, never a substitute for it — it only helps for
the failure modes an agent thinks to declare.

**A is not recommended as a separate design** — it is a strict subset of what C
already covers (C generalizes "claim then revert on the same task" to "no net status
change over the streak window," which includes A's case), so building it
independently would just be a second, narrower mechanism to maintain.
