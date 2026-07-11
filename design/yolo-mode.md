# Yolo mode: auto-approving founder gates, honestly

**Status: ACCEPTED (2026-07-10).** Founder decisions: no veto by default (fire next
tick; `--veto` remains available); lean ships UNTAGGED (safer default; its template
CONTEXT.md documents how to tag `merge`, and a dedicated pre-tagged "yolo" template
may come later); the board badge ships in BOTH `dais status` and `dais top` (one-off
panel line now, folded into the signal unification later). Companion to
`machine-model.md`.

## What it is

A founder-set, per-project, optionally time-boxed mode in which the dispatcher fires
founder gates automatically instead of parking work at them. The use case is real and
common: a prototype you want to run unattended over a weekend, a low-stakes project
where the review theater costs more than it catches, a founder who wants to watch the
loop run flat-out before deciding where the gates belong.

The design problem is that Dais's central claim is "the founder gates everything
outward." Yolo mode must be the deliberate, visible suspension of that claim, never a
quiet falsification of it. Three principles below make the difference.

## Principle 1: permissions can be skipped; facts can never be fabricated

Guards fall into two kinds:

- **Permission guards** say "I allow this." A bare founder edge, or one guarded by
  `confirm`, asserts nothing about the world. Auto-firing it is honest: the founder
  pre-granted the allowance when they turned yolo on.
- **Fact guards** say "a human verified this." `typed_confirm` exists to prove a human
  is present; `attest:<fact>` records human testimony about something the machine
  cannot check (for example: migrations applied). Auto-satisfying either would write a
  false record into the audit trail. The system would be testifying to things nobody
  verified.

**Rule: yolo auto-satisfies `confirm` and nothing else.** An edge with `typed_confirm`
or `attest:` parks exactly as it does today, yolo or not. If the founder wants that
edge automated too, the path is a conscious machine edit, made visible and diffable:
either downgrade the guard to `confirm`, or convert the attestation into a `verify:`
check the machine can actually run. Verifying a fact is the honest automation of it;
asserting it unverified is not. (`verify:` guards are not auto-granted by yolo either;
they already have their own honest mechanism.)

One consequence worth stating plainly: under this rule the stock coding machine's
release `greenlight` (typed_confirm + conditional attest) is **not yolo-able** as
shipped. That is correct. A founder who truly wants unattended deploys edits that
edge's guards down in their machine, where the decision is one `git diff` away from
being noticed, rather than flipping a flag that silently converts "nothing deploys
without me" into "everything deploys without me."

## Principle 2: two keys, both explicit

Yolo requires two independent opt-ins, one authored and one operational:

1. **The machine names which edges yolo may fire.** An edge gains an optional
   `"yolo": true` tag. This solves edge *selection*: a gate state usually has several
   founder edges (approve, request_changes, reject, defer) and only the machine author
   knows which one is "the default action under autopilot." No heuristics, no verb
   name matching; the machine is authored, so this is authored too.
2. **The founder turns the mode on per project, at runtime.** A marker file,
   `projects/<p>/.yolo`, in the same runtime-state idiom as `.paused`,
   `.model-<role>.exhausted`, and `.stalled-<role>`. Config in project.yaml stays
   declarative; yolo is operational state with an expiry, so it is a marker.

Neither key alone does anything. A tagged machine with no marker behaves exactly as
today. A marker on a project whose machine tags nothing fires nothing.

## Principle 3: loud, audited, and self-expiring

- **Board badge.** `dais status` (and `dais top`, once the panel unifies these
  signals) shows `⚡ YOLO` on the project line, with expiry and veto delay when set.
  A mode that suspends governance must be impossible to forget.
- **Audit line per auto-fire.** Every yolo-fired edge appends an attributed notes
  entry to the task: `[yolo <ts>] auto-fired <verb> (founder gate auto-approved;
  mode expires <ts|never>)`. The task log, which is the coordination record, never
  shows a gate silently passed.
- **Time-box by default in the UX.** `dais yolo <p> on --for 48h` writes an expiry
  epoch into the marker; the dispatcher deletes an expired marker and journals it.
  A bare `on` (no expiry) is allowed but the CLI echoes a nudge. Nobody should
  discover in March that a project has been ungated since January.
- **Optional veto window.** `--veto 30m` in the marker makes the sweep skip tasks
  whose `updated_at` is younger than the window: the gate auto-fires only after the
  task has sat untouched that long, giving the founder a standing chance to catch one
  before it goes. Any task activity (a note, a re-rank) resets the clock, which errs
  in the safe direction. Default: no veto, fire on the next tick.

## Mechanics

**The sweep** joins the dispatcher's existing maintenance pass (beside `recover` and
`advance` in `dispatch.sh`), one engine call per yolo project per tick:

```
machine.py yolo <db> <machine.json> <project> [--veto-min N]
```

For each non-terminal task in the project: find edges from its state tagged
`"yolo": true` whose actor is a human role (`founder` or a `human: true` role) and
whose guards are a subset of `{confirm}`. If the veto window passes, fire the edge via
the normal `fire()` path with `ctx={"confirm": True}`, actor = the edge's `by`. All
existing semantics hold for free: CAS state changes, guards re-checked inside `fire`,
effects (spawn, aggregate, then) run atomically, `@history` behaves. Fired task ids
print one per line; dispatch.sh journals them (`tlog "yolo[<p>]: fired <id>:<verb>"`).
A tagged edge whose guards include a strong-human guard is skipped at runtime (and is
a lint error, below), so a machine edited after the marker was set cannot smuggle an
attestation past the rule.

**The marker** is one line: `<expiry-epoch-or-0> [veto-minutes]`. Dispatch reads it,
deletes it when expired, and passes the veto through. `dais yolo <p> off` removes it.

**Ordering.** The sweep runs before eligibility so work freed by an auto-approval
dispatches the same tick, matching how `advance` already works.

## Lint

- **New error, E-class:** an edge tagged `"yolo": true` carrying `typed_confirm` or
  `attest:` is a config contradiction. Message: "yolo cannot auto-satisfy a
  strong-human guard; downgrade the guard consciously or untag the edge."
- **New warning, W-class:** an edge tagged `"yolo": true` that is outward (its own
  `script.outward`, or W3's approach analysis) draws "yolo'd outward edge: publishes
  or deploys with no human in the loop while yolo is on." Warn, not error; that
  choice is legitimate and the point of the mode, but it must be conscious.
- Existing W3 is untouched. It already polices outward edges lacking strong-human
  gates, which is exactly the analysis that keeps yolo-eligible outward edges rare.

## Stock template defaults

- **coding:** tag `approve` (proposal_review) `"yolo": true`. Yolo on a coding project
  means proposals flow, the build rail runs, QA still verifies, and releases still
  park at the un-yolo-able greenlight. That is a good default meaning for "let it
  run": full-speed inward, gated outward.
- **lean:** ships UNTAGGED (founder decision: safer default for a template pointed
  at real repos, where merge can equal deploy). The template CONTEXT.md documents the
  one-line tag (`"yolo": true` on the `merge` edge) that turns lean+yolo into the
  fully autonomous single-agent configuration; a dedicated pre-tagged template may
  come later.
- **advisory:** tag nothing. Auto-accepting recommendations is decision theater; you
  cannot delegate your own judgment to the system that is asking for it.
- **marketing:** tag nothing. Publishing is outward; tagging it is the machine
  author's deliberate act, prompted by the W-warning above.

## What yolo deliberately does not do

- Does not satisfy `typed_confirm`, `attest:`, or `verify:` guards, ever.
- Does not fire untagged founder edges, defer/undefer, cancel, or `note` acknowledge
  (unless an author tags them, which lint allows for confirm-or-bare edges).
- Does not exist workspace-wide. Per project only; five markers if you mean five.
- Does not touch the machine, the audit trail's honesty, or the `--by` plumbing
  (manual `--by founder` fires remain what they are today: the founder's own
  automation hook, out of scope here).
- Does not suppress the stall/throttle machinery; fewer parked states simply mean
  fewer stalls.

## CLI

```
dais yolo <project> on [--for 48h] [--veto 30m]   # write the marker (echo a nudge if no --for)
dais yolo <project> off                            # remove it
dais yolo <project>                                # status: on/off, expiry, veto, eligible edges
```

The status form also lists which of the project's edges are yolo-eligible right now
(tagged + confirm-only), which is the founder's preview of what the mode will actually
do before turning it on.

## Size and touch points

| Piece | Where | Est. |
|---|---|---|
| `yolo` engine subcommand (select + fire + audit note) | machine.py | ~45 lines |
| dispatcher sweep + marker TTL | dispatch.sh | ~15 |
| CLI verb + duration parsing | dais | ~35 |
| lint E + W rules | machine.py | ~12 |
| status badge | dashboard.py | ~8 |
| template tags + notes | coding/lean machine.json | 2 edits |
| tests (fire, refuse-strong-guard, veto, expiry, lint) | tests/ | ~70 |
| docs (README section, machine-model.md `yolo` key) | docs | prose |

No schema change. No new daemon. One new marker idiom instance, three existing ones
as precedent.

## Open questions for the founder

1. **Veto default:** ship with no veto (fire next tick) or a conservative default like
   15m that `--veto 0` removes?
2. **lean's merge tag:** ship lean with merge pre-tagged (as specced), or ship it
   untagged and let the CONTEXT.md tell founders how to tag it? Pre-tagged is the
   better demo; untagged is the safer default for a template pointed at real repos.
3. **Badge in `dais top`:** fold into the planned panel/status signal unification, or
   worth a one-off panel line at ship time?
