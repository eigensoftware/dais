#!/usr/bin/env python3
"""Coherence lint for an authored task state-machine (design/machines/*.json).

Philosophy (per the design): the structure imposes NO policy limits. Lint only
checks COHERENCE — that the machine is well-formed and runs deterministically.
Anything policy/safety-flavored is a WARNING that never blocks. Green errors =
the founder may build whatever shape they want.

  ERRORS (block — the machine is malformed / non-deterministic):
    E1 referential integrity — every from/to state, `by` role, spawn target,
       and guard resolves.
    E2 no dead-end — every non-terminal state has >=1 outgoing edge.
    E3 unambiguous dispatch — a state auto-dispatches at most one agent role
       (founder/system don't count; an explicit {"pool": true} opts into any-of).
    E4 has >=1 initial and >=1 terminal state.

  WARNINGS (advisory — never block):
    W1 state unreachable from any initial.
    W2 state cannot reach any terminal (possible strand/infinite loop).
    W3 outward-effect edge with no strong human guard on it or its approach
       (safety-by-omission — you *may* auto-fire to prod; did you mean to?).

Stdlib only. Loads JSON (a strict YAML subset); a production loader would read
the founder-facing .yaml.
"""
import json
import sys

IMPLICIT_ACTORS = {"founder", "system"}         # not scheduled as agents
STRONG_HUMAN_GUARDS = ("typed_confirm", "attest:")   # can't be auto-satisfied
GUARD_PREFIXES = ("verify:", "attest:", "role:")
GUARD_ATOMS = {"confirm", "typed_confirm", "unblocked"}
EFFECT_KINDS = {"spawn", "aggregate", "script", "then"}


def _guard_ok(g):
    return g in GUARD_ATOMS or g.split(" ")[0].startswith(GUARD_PREFIXES) \
        or any(g.startswith(p) for p in GUARD_PREFIXES)


def _is_strong_human(g):
    return g == "typed_confirm" or g.startswith("attest:")


def lint(m):
    errors, warns = [], []
    states = m.get("states", {})
    roles = set(m.get("roles", {})) | IMPLICIT_ACTORS
    edges = m.get("edges", [])
    terminals = {s for s, meta in states.items() if meta.get("terminal")}
    initials = {s for s, meta in states.items() if meta.get("initial")}
    out = {s: [] for s in states}

    # E1 referential integrity
    for i, e in enumerate(edges):
        tag = f"edge[{i}] {e.get('from','?')}--{e.get('verb','?')}-->{e.get('to','?')}"
        if e.get("from") not in states:
            errors.append(f"E1 {tag}: unknown `from` state {e.get('from')!r}")
        else:
            out[e["from"]].append(e)
        if e.get("to") not in states:
            errors.append(f"E1 {tag}: unknown `to` state {e.get('to')!r}")
        if e.get("by") not in roles:
            errors.append(f"E1 {tag}: unknown actor `by` {e.get('by')!r}")
        for g in e.get("guards", []):
            if not _guard_ok(g):
                errors.append(f"E1 {tag}: unknown guard {g!r}")
        eff = e.get("effect", {})
        for k in eff:
            if k not in EFFECT_KINDS:
                errors.append(f"E1 {tag}: unknown effect kind {k!r}")
        sp = eff.get("spawn")
        if sp:
            if sp.get("initial") not in states:
                errors.append(f"E1 {tag}: spawn.initial {sp.get('initial')!r} is not a state")
            if sp.get("by") not in roles:
                errors.append(f"E1 {tag}: spawn.by {sp.get('by')!r} is not a role")

    # E2 no dead-end
    for s in states:
        if s not in terminals and not out.get(s):
            errors.append(f"E2 dead-end: non-terminal state {s!r} has no outgoing edge")

    # E3 unambiguous dispatch
    for s in states:
        if states[s].get("pool"):
            continue
        agents = {e["by"] for e in out.get(s, []) if e.get("by") not in IMPLICIT_ACTORS}
        if len(agents) > 1:
            errors.append(f"E3 ambiguous dispatch: state {s!r} auto-dispatches "
                          f"multiple roles {sorted(agents)} (add \"pool\": true to allow)")

    # E4 endpoints
    if not initials:
        errors.append("E4: no `initial` state declared")
    if not terminals:
        errors.append("E4: no `terminal` state declared")

    # W1 reachable from an initial
    seen, stack = set(initials), list(initials)
    while stack:
        s = stack.pop()
        for e in out.get(s, []):
            if e["to"] in states and e["to"] not in seen:
                seen.add(e["to"]); stack.append(e["to"])
    for s in states:
        if s not in seen:
            warns.append(f"W1 unreachable: {s!r} is not reachable from any initial state")

    # W2 can reach a terminal (reverse BFS from terminals)
    preds = {s: [] for s in states}
    for e in edges:
        if e.get("from") in states and e.get("to") in states:
            preds[e["to"]].append(e["from"])
    can_end, stack = set(terminals), list(terminals)
    while stack:
        s = stack.pop()
        for p in preds.get(s, []):
            if p not in can_end:
                can_end.add(p); stack.append(p)
    for s in states:
        if s not in can_end:
            warns.append(f"W2 no exit: {s!r} cannot reach any terminal state")

    # W3 outward effect without a strong human guard on the edge or its approach
    inbound = {s: [] for s in states}
    for e in edges:
        if e.get("to") in states:
            inbound[e["to"]].append(e)
    for e in edges:
        eff = e.get("effect", {})
        script = eff.get("script")
        if not (isinstance(script, dict) and script.get("outward")):
            continue
        guarded_here = any(_is_strong_human(g) for g in e.get("guards", []))
        guarded_approach = any(_is_strong_human(g)
                               for ie in inbound.get(e.get("from"), [])
                               for g in ie.get("guards", []))
        if not (guarded_here or guarded_approach):
            warns.append(f"W3 unguarded outward effect: {e.get('from')}--{e.get('verb')}-->"
                         f"{e.get('to')} runs an outward script with no human gate on it or "
                         f"its approach (may auto-fire to prod)")
    return errors, warns


def main(path):
    with open(path) as f:
        m = json.load(f)
    errors, warns = lint(m)
    print(f"lint: {m.get('name', path)}  —  {len(states_count(m))} states, {len(m.get('edges', []))} edges")
    for w in warns:
        print(f"  ⚠  {w}")
    for e in errors:
        print(f"  ✗  {e}")
    if not errors:
        print(f"  ✓ coherent" + (f" ({len(warns)} advisory warning(s))" if warns else ""))
    return 1 if errors else 0


def states_count(m):
    return m.get("states", {})


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "design/machines/coding.machine.json"))
