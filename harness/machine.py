"""Dais task state-machine engine — the authored-workflow core (design/machine-model.md).

One atom (task), two graphs (lifecycle = this machine; composition = task_links),
one bridge (transition EFFECTS: spawn / aggregate / then). The load-bearing
invariant: every state change is a transition — effects FIRE EDGES, they never
poke state. So spawned/batched work stays inside the machine and stays lint-able.

Stdlib only (matches the harness). Machines load from JSON today (a strict YAML
subset); a production loader would read the founder-facing .yaml.

Shelled by the `dais` CLI via __main__ (like actions.py); importable by tests and
the scheduler.
"""
import json
import os
import sqlite3
import sys

IMPLICIT_ACTORS = {"founder", "system"}          # never scheduled as agents
GUARD_ATOMS = {"confirm", "typed_confirm", "unblocked"}
GUARD_PREFIXES = ("verify:", "attest:", "role:")
EFFECT_KINDS = {"spawn", "aggregate", "script", "then"}


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #
def load(path):
    with open(path) as f:
        return json.load(f)


def default_machine_path(root=None, project_machine=None):
    """Resolve a machine file: explicit path -> <builtin>/<name>.machine.json -> coding. Built-in
    machines ship with the TOOL, so resolve them relative to THIS module (DAIS_ROOT/harness/machines)
    — not the passed `root`, which callers variously pass as DAIS_ROOT or DAIS_HOME (the workspace)."""
    builtin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "machines")
    if project_machine:
        for cand in (project_machine,
                     os.path.join(builtin, f"{project_machine}.machine.json")):
            if os.path.exists(cand):
                return cand
    return os.path.join(builtin, "coding.machine.json")


def project_machine_path(home, project, ref=None):
    """The machine a project runs: its OWN projects/<project>/machine.json (seeded from a workflow
    template at scaffold, editable per project), else a named/default built-in. This is why a
    scaffolded project is machine-driven with no `machine:` gate — it just has its machine file."""
    local = os.path.join(home, "projects", project, "machine.json")
    if os.path.exists(local):
        return local
    return default_machine_path(None, ref)


# --------------------------------------------------------------------------- #
# lint — coherence only (errors block); policy/safety is advisory (warns)
# --------------------------------------------------------------------------- #
def _guard_ok(g):
    return g in GUARD_ATOMS or any(g.startswith(p) for p in GUARD_PREFIXES)


def _is_strong_human(g):
    return g == "typed_confirm" or g.startswith("attest:")


def lint(m):
    """Return (errors, warnings). Errors mean the machine is malformed / non-deterministic.
    One function per rule below — adding a rule is adding a pass, not growing a body."""
    errors, warns = [], []
    states = m.get("states", {})
    edges = m.get("edges", [])
    out = {s: [] for s in states}
    for e in edges:
        if e.get("from") in states:
            out[e["from"]].append(e)
    _lint_e1_references(m, states, edges, errors)
    _lint_e2_dead_ends(states, out, errors)
    _lint_e3_dispatch(states, out, errors)
    _lint_e4_entry_exits(m, states, errors)
    _lint_e5_duplicate_edges(edges, errors)
    _lint_w1_reachability(states, edges, out, warns)
    _lint_w2_terminal_reach(states, edges, warns)
    _lint_w3_unguarded_outward(states, edges, warns)
    return errors, warns


def _lint_e1_references(m, states, edges, errors):
    """E1 referential integrity: every from/to/by/guard/effect names something that exists."""
    roles = set(m.get("roles", {})) | IMPLICIT_ACTORS
    for i, e in enumerate(edges):
        tag = f"edge[{i}] {e.get('from','?')}--{e.get('verb','?')}-->{e.get('to','?')}"
        if e.get("from") not in states:
            errors.append(f"E1 {tag}: unknown `from` state {e.get('from')!r}")
        if e.get("to") == "@history":
            # history return: valid only out of a state that DECLARES history (else there is
            # nothing recorded to return to), and its fallback must be a real state.
            if not states.get(e.get("from"), {}).get("history"):
                errors.append(f"E1 {tag}: '@history' target from a state without \"history\": true")
            if e.get("default", "ready") not in states:
                errors.append(f"E1 {tag}: '@history' fallback {e.get('default')!r} is not a state")
        elif e.get("to") not in states:
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


def _lint_e2_dead_ends(states, out, errors):
    """E2: every non-terminal state must have a way out."""
    for s in states:
        if not states[s].get("terminal") and not out.get(s):
            errors.append(f"E2 dead-end: non-terminal state {s!r} has no outgoing edge")


def _lint_e3_dispatch(states, out, errors):
    """E3: at most one agent role dispatches a state (unless it opts into pool: any-of-N)."""
    for s in states:
        if states[s].get("pool"):
            continue
        agents = {e.get("by") for e in out.get(s, [])
                  if e.get("by") and e.get("by") not in IMPLICIT_ACTORS}
        if len(agents) > 1:
            errors.append(f"E3 ambiguous dispatch: state {s!r} auto-dispatches multiple roles "
                          f"{sorted(agents)} (add \"pool\": true to allow any-of)")


def _lint_e4_entry_exits(m, states, errors):
    """E4: the machine has somewhere to start (initial + valid entry) and somewhere to end."""
    if not any(meta.get("initial") for meta in states.values()):
        errors.append("E4: no `initial` state declared")
    if not any(meta.get("terminal") for meta in states.values()):
        errors.append("E4: no `terminal` state declared")
    if m.get("entry") and m["entry"] not in states:
        errors.append(f"E4: `entry` {m['entry']!r} is not a state (new tasks would enter an "
                      f"unknown state with no edges)")


def _lint_e5_duplicate_edges(edges, errors):
    """E5: a duplicate (from, verb) pair is non-deterministic — fire() takes the first match,
    silently shadowing the other edge (and whatever guards/effects it carries)."""
    seen_fv = {}
    for e in edges:
        fv = (e.get("from"), e.get("verb"))
        if all(fv) and fv in seen_fv:
            errors.append(f"E5 duplicate edge: {fv[0]!r} --{fv[1]}--> appears more than once "
                          f"(-> {seen_fv[fv]!r} and -> {e.get('to')!r}); the second is unreachable")
        elif all(fv):
            seen_fv[fv] = e.get("to")


def _lint_w1_reachability(states, edges, out, warns):
    """W1: entry points are declared initials PLUS any state a spawn effect drops a task into."""
    initials = {s for s, meta in states.items() if meta.get("initial")}
    spawn_targets = {e["effect"]["spawn"]["initial"] for e in edges
                     if e.get("effect", {}).get("spawn", {}).get("initial") in states}
    seen = initials | spawn_targets
    stack = list(seen)
    while stack:
        for e in out.get(stack.pop(), []):
            if e["to"] in states and e["to"] not in seen:
                seen.add(e["to"]); stack.append(e["to"])
    for s in states:
        if s not in seen:
            warns.append(f"W1 unreachable: {s!r} not reachable from an initial state or a spawn")


def _lint_w2_terminal_reach(states, edges, warns):
    """W2: every state should have a path to some terminal (else work strands there)."""
    preds = {s: [] for s in states}
    for e in edges:
        if e.get("from") in states and e.get("to") in states:
            preds[e["to"]].append(e["from"])
    can_end = {s for s, meta in states.items() if meta.get("terminal")}
    stack = list(can_end)
    while stack:
        for p in preds.get(stack.pop(), []):
            if p not in can_end:
                can_end.add(p); stack.append(p)
    for s in states:
        if s not in can_end:
            warns.append(f"W2 no exit: {s!r} cannot reach any terminal state")


def _lint_w3_unguarded_outward(states, edges, warns):
    """W3: an outward script effect wants a strong-human guard on its edge or its approach."""
    inbound = {s: [] for s in states}
    for e in edges:
        if e.get("to") in states:
            inbound[e["to"]].append(e)
    for e in edges:
        sc = e.get("effect", {}).get("script")
        if not (isinstance(sc, dict) and sc.get("outward")):
            continue
        here = any(_is_strong_human(g) for g in e.get("guards", []))
        approach = any(_is_strong_human(g) for ie in inbound.get(e.get("from"), [])
                       for g in ie.get("guards", []))
        if not (here or approach):
            warns.append(f"W3 unguarded outward effect: {e.get('from')}--{e.get('verb')}-->"
                         f"{e.get('to')} runs an outward script with no human gate on it or its "
                         f"approach (may auto-fire to prod)")


# --------------------------------------------------------------------------- #
# engine — introspection
# --------------------------------------------------------------------------- #
def edges_from(m, state):
    return [e for e in (m or {}).get("edges", []) if e.get("from") == state]



def release_lane(m):
    """(open_state, pool_state, lane_states) for the machine's release lane — the from-state of
    the first edge carrying an `aggregate` effect, the state its select clause pools (what
    assemble sweeps), and every non-terminal state reachable from open_state (the lane itself,
    for is-a-release-in-flight checks). (None, None, set()) when the machine has no lane."""
    for e in (m or {}).get("edges", []):
        eff = e.get("effect") or {}
        for x in (eff if isinstance(eff, list) else [eff]):
            if isinstance(x, dict) and "aggregate" in x:
                sel = (x["aggregate"] or {}).get("select", "")
                pool = sel.split("=", 1)[1].strip() if "=" in sel else "approved"
                start = e["from"]
                lane, todo = set(), [start]
                while todo:
                    st = todo.pop()
                    if st in lane:
                        continue
                    lane.add(st)
                    for e2 in edges_from(m, st):
                        to = e2.get("to")
                        if to and not m.get("states", {}).get(to, {}).get("terminal"):
                            todo.append(to)
                return start, pool, lane
    return None, None, set()


def dispatch_role(m, state):
    """The agent role the scheduler launches for a task in `state`, or None (parks for a human /
    awaits a system edge). One agent actor → that role. Multiple actors is an E3 lint error UNLESS
    the state is tagged `pool: true` (any-of-N dispatch): then the pick is deterministic — the
    first pool member in the machine's `roles` declaration order (declaration order = precedence)."""
    states = (m or {}).get("states", {})
    if state not in states:
        return None
    agents = {e.get("by") for e in edges_from(m, state)
              if e.get("by") and e.get("by") not in IMPLICIT_ACTORS}
    if len(agents) == 1:
        return next(iter(agents))
    if len(agents) > 1 and states[state].get("pool"):
        for r in (m or {}).get("roles", {}):
            if r in agents:
                return r
        return sorted(agents)[0]
    return None


def _find_edge(m, state, verb):
    for e in edges_from(m, state):
        if e.get("verb") == verb:
            return e
    return None


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _dep_open(conn, tid):
    """True if the task waits on a tasks.blocked_on predecessor that isn't done/cancelled. A dangling
    ref (deleted predecessor) or a pre-migration DB with no blocked_on column counts as unblocked, so
    work is never stranded. (This is the cross-task dependency; the `blocked` STATE + blocks_parent
    links are separate and handled by dispatch_role/_blockers_open.)"""
    try:
        row = conn.execute("SELECT blocked_on FROM tasks WHERE id=?", (tid,)).fetchone()
    except sqlite3.Error:
        return False
    dep = row[0] if row else None
    if not dep:
        return False
    pred = conn.execute("SELECT status FROM tasks WHERE id=?", (dep,)).fetchone()
    return bool(pred) and pred[0] not in ("done", "cancelled")


def next_role(conn, m, project, excluded=frozenset()):
    """Reactive dispatch (the scheduler): the role to launch next for this project — the dispatch
    role of the highest-priority pending task sitting in a state that auto-dispatches. '' when
    nothing is dispatchable (the caller then considers cadence roles). Blocked/parked/gate states
    have no dispatch role, so they're naturally skipped; a task waiting on an open dependency
    (tasks.blocked_on) is skipped too. `excluded` roles are skipped WHILE SCANNING (the next
    dispatchable task of a different role still surfaces) — how the dispatcher's no-progress
    throttle avoids starving a whole project on one cooled role."""
    rows = conn.execute("SELECT id, status, COALESCE(priority,'medium') FROM tasks "
                        "WHERE project=? AND status NOT IN ('done','cancelled')", (project,)).fetchall()
    best = None
    for r in rows:
        rid = r["id"] if hasattr(r, "keys") else r[0]
        status = r["status"] if hasattr(r, "keys") else r[1]
        role = dispatch_role(m, status)
        if not role or role in excluded or _dep_open(conn, rid):
            continue
        prio = (r["COALESCE(priority,'medium')"] if hasattr(r, "keys") else r[2])
        key = (_PRIORITY_RANK.get(prio, 2), rid)
        if best is None or key < best[0]:
            best = (key, role)
    return best[1] if best else ""


# --------------------------------------------------------------------------- #
# display derivation — so `top` configures itself from the machine (no per-model wiring)
# --------------------------------------------------------------------------- #
BAND_ORDER = ["NEEDS YOU", "QUEUED", "WAITING", "ARCHIVE"]


def band_of(m, state):
    """Which board band a state belongs to, DERIVED from its role in the machine. An optional
    per-state `band` overrides the derivation for edge cases (e.g. deferred -> WAITING)."""
    meta = (m or {}).get("states", {}).get(state, {})
    if meta.get("band"):
        return meta["band"]
    if meta.get("terminal"):
        return "ARCHIVE"
    if dispatch_role(m, state):
        return "QUEUED"
    if any(e.get("by") == "founder" for e in edges_from(m, state)):
        return "NEEDS YOU"
    return "WAITING"


def bands(m):
    """Ordered {band: [states]} for the whole machine. RUNNING is a live-run overlay the TUI adds
    separately (it's about locks, not state). Founder-defined custom bands are appended after the
    canonical order."""
    out = {}
    for s in m.get("states", {}):
        out.setdefault(band_of(m, s), []).append(s)
    ordered = {b: out[b] for b in BAND_ORDER if b in out}
    for b, ss in out.items():
        ordered.setdefault(b, ss)
    return ordered


def edge_actions(m, state):
    """Actions available on a task in `state`, derived from its outgoing edges — replaces the
    hardcoded actions.py catalog. `__start` is the synthetic 'launch the dispatch agent' action;
    every other action fires an edge. `confirm` flags whether the UI must prompt (any guard needing
    a human input)."""
    acts = []
    d = dispatch_role(m, state)
    if d:
        acts.append({"verb": "__start", "label": f"start ({d})", "by": d, "dispatch": True})
    for e in edges_from(m, state):
        if e.get("by") == "system":
            continue
        guards = e.get("guards", [])
        acts.append({
            "verb": e["verb"], "to": e["to"], "by": e["by"], "guards": guards,
            "human": e["by"] == "founder",
            "confirm": any(g in ("confirm", "typed_confirm") or g.startswith("attest:") for g in guards),
            "label": e["verb"].replace("_", " ") + f" → {e['to']}",
        })
    return acts


# --------------------------------------------------------------------------- #
# engine — firing (all state change goes through here)
# --------------------------------------------------------------------------- #
class GuardFailure(Exception):
    pass


def task_row(conn, tid):
    """The engine's view of a task (public: UIs fetch through this too, so guard predicates
    like attest_fact/prompts_for see the same columns enforcement sees).
    touches_migrations (0005) and parked_from (0004) arrive by migration — degrade gracefully on an
    unmigrated db by peeling the newest columns off the SELECT until one succeeds."""
    for cols in ("id,project,status,title,assignee,parked_from,touches_migrations",
                 "id,project,status,title,assignee,parked_from",
                 "id,project,status,title,assignee"):
        try:
            return conn.execute(f"SELECT {cols} FROM tasks WHERE id=?", (tid,)).fetchone()
        except sqlite3.OperationalError:
            continue
    return None


def _prefix_candidates(name):
    """Deterministic prefix candidates for a project name, most distinctive first: for a
    multi-word name, first-word[:3] + the next word's initial (puttflow-web -> putw, so it
    can't be misread as a puttflow id); then name[:4]; then letter-substitution variants;
    then a numbered fallback. Always lowercase alphanumeric."""
    import re as _re
    words = [w for w in _re.split(r"[^a-z0-9]+", name.lower()) if w]
    base = words[0] if words else "task"
    out = []
    if len(words) > 1 and len(base) >= 3:
        out.append(base[:3] + words[1][0])
    out.append(base[:4])
    for c in base[4:] + "".join(words[1:]):
        out.append(base[:3] + c)
    if len(words) > 1:                       # stem taken entirely? initial + next word reads
        out.append((base[0] + words[1][:3]))  # far better than a char-drop: puttflow-web -> pweb
    head = base[:4]
    for i in range(len(head) - 1, -1, -1):   # char-dropped variants: escape a taken stem
        out.append(head[:i] + head[i + 1:])  # entirely (dais with dai- taken -> das)
    out += [base[:3] + str(i) for i in range(2, 100)]
    seen, uniq = set(), []
    for p in out:
        if len(p) >= 2 and p not in seen:
            seen.add(p); uniq.append(p)
    return uniq


def _id_prefix(conn, project):
    """The auto-id prefix this project's tasks use — unique per project. A project KEEPS the
    prefix it owns (its oldest task's token, when the overall-oldest task with that token is
    also this project's — ids are identity, history never re-labels). A collision loser, or a
    fresh project, derives from _prefix_candidates, skipping tokens other projects' ids use."""
    row = conn.execute("SELECT id FROM tasks WHERE project=? AND id LIKE '%-%' "
                       "ORDER BY rowid LIMIT 1", (project,)).fetchone()
    if row:
        token = row[0].split("-")[0]
        owner = conn.execute("SELECT project FROM tasks WHERE id LIKE ? ORDER BY rowid LIMIT 1",
                             (token + "-%",)).fetchone()
        if owner and owner[0] == project:
            return token
    taken = {r[0].split("-")[0] for r in
             conn.execute("SELECT DISTINCT id FROM tasks WHERE project<>? AND id LIKE '%-%'",
                          (project,))}
    for cand in _prefix_candidates(project):
        # PREFIX-FREE, not merely unequal: dai- taken makes dais- a conflict (and vice
        # versa) — dai-9 vs dais-9 is visually ambiguous even though the strings differ.
        if not any(t.startswith(cand) or cand.startswith(t) for t in taken):
            return cand
    return project[:3]                                   # unreachable in practice; backstop


def _new_id(conn, project):
    abbr = _id_prefix(conn, project)
    n = conn.execute("SELECT COUNT(*)+1 FROM tasks WHERE project=?", (project,)).fetchone()[0]
    tid = f"{abbr}-{n}"
    while conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        n += 1; tid = f"{abbr}-{n}"
    return tid


def _insert_task(conn, project, title, status, assignee, tid=None, extra=None):
    """INSERT a task and return its id. With no `tid` a fresh auto-id is allocated, retrying on
    a collision — two concurrent creators (parallel agents adding tasks, racing spawn effects)
    compute the same next id; the loser re-derives instead of dying on the UNIQUE constraint.
    An EXPLICIT `tid` collision is a real error (ValueError), never a retry. `extra` adds
    columns (priority/notes/blocked_on) — keys are code-controlled, values are bound."""
    extra = extra or {}
    cols = ["id", "project", "title", "status", "assignee"] + list(extra)
    sql = f"INSERT INTO tasks({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    if tid is not None:
        try:
            conn.execute(sql, [tid, project, title, status, assignee] + list(extra.values()))
            return tid
        except sqlite3.IntegrityError:
            raise ValueError(f"could not insert {tid} (id already exists)")
    for _ in range(20):
        tid = _new_id(conn, project)
        try:
            conn.execute(sql, [tid, project, title, status, assignee] + list(extra.values()))
            return tid
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError(f"could not allocate a task id for project {project!r}")


def _blockers_open(conn, tid):
    rows = conn.execute("SELECT child_id FROM task_links WHERE parent_id=? AND rel='blocks_parent'",
                        (tid,)).fetchall()
    for (child,) in rows:
        r = conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()
        if r and r[0] not in ("done", "cancelled"):
            return True
    return False


def _run_check(m, check, task):
    """Run the machine's declared checker command for `verify:<check>` (the machine's `checks`
    map: {name: shell command}). True = passed (exit 0). None = no checker declared, so the
    guard can only be satisfied by an explicit --verify self-assertion from the firing role."""
    cmd = (m or {}).get("checks", {}).get(check)
    if not cmd:
        return None
    import subprocess
    env = dict(os.environ, DAIS_TASK=task["id"], DAIS_PROJECT=task["project"] or "")
    try:
        return subprocess.run(cmd, shell=True, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def attest_fact(guard, task):
    """The ONE reading of an `attest:<fact>[ when task:<flag>]` guard, shared by the engine
    (which enforces it) and the panel (which decides whether to PROMPT for it) — a second
    hand-rolled parse is how the two layers drift apart. Returns the fact that must be
    attested for this task row, or None when the guard isn't an attest or its conditional
    lifts it. The conditional lifts ONLY on a flag that is present AND explicitly falsy —
    truthy, NULL, a missing column, or no row at all still require the attest (fail-safe:
    the gate can never be skipped by omission). The release greenlight uses this: `assemble`
    records `touches_migrations`, so a migration-free release doesn't demand a vacuous
    migrations attestation."""
    if not guard.startswith("attest:"):
        return None
    fact, _, cond = guard[len("attest:"):].partition(" ")
    cond = cond.strip()
    if cond.startswith("when task:") and task is not None:
        key = cond[len("when task:"):].strip()
        val = task[key] if key in task.keys() else None
        if val is not None and not val:              # column present AND falsy → requirement lifts
            return None
    return fact


def prompts_for(m, edge, task):
    """What a HUMAN must supply to fire this edge on this task — the one reading of the guard
    list for elicitation, next to _check_guards' reading for enforcement, so a UI can never
    prompt for something fire() won't demand (or skip something it will). Ordered entries:
      {"kind": "typed"}                             — type the task id (typed_confirm)
      {"kind": "attest", "fact": f}                 — assert the fact; conditional attests are
                                                      resolved against the task row (lifted ones
                                                      are ABSENT, same rule as enforcement)
      {"kind": "confirm"}                           — a click
      {"kind": "verify", "check": c, "declared": b} — b: the machine declares a checker command
                                                      (fire runs it); False = only an explicit
                                                      --verify self-assertion passes (fails closed)
    """
    out, checks = [], (m or {}).get("checks", {})
    for g in edge.get("guards", []):
        if g == "typed_confirm":
            out.append({"kind": "typed"})
        elif g == "confirm":
            out.append({"kind": "confirm"})
        elif g.startswith("attest:"):
            fact = attest_fact(g, task)
            if fact:
                out.append({"kind": "attest", "fact": fact})
        elif g.startswith("verify:"):
            check = g[len("verify:"):]
            out.append({"kind": "verify", "check": check, "declared": check in checks})
    return out


def _check_guards(conn, m, edge, task, ctx):
    for g in edge.get("guards", []):
        if g == "confirm":
            if not ctx.get("confirm"):
                raise GuardFailure(f"guard `confirm` unmet (needs --confirm)")
        elif g == "typed_confirm":
            if ctx.get("typed") != task["id"]:
                raise GuardFailure(f"guard `typed_confirm` unmet (must type the task id {task['id']!r})")
        elif g == "unblocked":
            if _blockers_open(conn, task["id"]):
                raise GuardFailure("guard `unblocked` unmet (open blocks_parent children remain)")
        elif g.startswith("attest:"):
            fact = attest_fact(g, task)
            if fact and not (ctx.get("attest") or {}).get(fact):
                raise GuardFailure(f"guard `attest:{fact}` unmet (needs --attest {fact})")
        elif g.startswith("verify:"):
            # an explicit --verify (the firing role asserting it ran the check) wins; else the
            # machine's declared checker command runs; a check with neither fails closed.
            check = g[len("verify:"):]
            v = (ctx.get("verifiers") or {}).get(check)
            if v is None:
                v = _run_check(m, check, task)
            if not v:
                raise GuardFailure(f"guard `verify:{check}` unmet (checker returned false/absent)")
        elif g.startswith("role:"):
            if task["assignee"] != g[len("role:"):]:
                raise GuardFailure(f"guard `role:` unmet")


def fire(conn, m, tid, verb, actor, ctx=None, _nested=False):
    """Fire the (state,verb) edge as `actor`. Validates actor + guards, applies the state change,
    then runs effects — which themselves fire edges. Returns a result dict. Raises GuardFailure.

    ATOMIC: the transition and ALL its effects (spawns, links, nested `then` fires) commit
    together or not at all — a failing effect rolls the whole fire back, so the DB never shows
    a half-applied release. Concurrency-safe: the state change is a compare-and-swap
    (`WHERE status=<from>`), so two racing fires can't both apply (and double-run effects) —
    the loser sees the task moved and fails cleanly."""
    ctx = ctx or {}
    top = not _nested
    if top:
        # SAVEPOINT (not BEGIN) so a caller's open transaction is joined, not clobbered;
        # standalone it opens one. Rollback-to-savepoint undoes only this fire's work.
        conn.execute("SAVEPOINT dais_fire")
    try:
        task = task_row(conn, tid)
        if not task:
            raise ValueError(f"no such task: {tid}")
        # The dependency chain binds AGENTS, not just the dispatcher: an agent may not fire any
        # edge on a task whose blocked_on predecessor is still open (that hole let chained work
        # get built early). The founder and system edges still act (defer/cancel/unblock = surgery).
        if actor not in ("founder", "system") and _dep_open(conn, tid):
            raise GuardFailure(f"{tid} is blocked on an open dependency — agents can't fire it "
                               f"until the predecessor is done (founder: dais task set {tid} "
                               f"--depends-on '' to unchain)")
        edge = _find_edge(m, task["status"], verb)
        if not edge:
            raise ValueError(f"no edge {verb!r} from state {task['status']!r} for task {tid}")
        if actor != edge.get("by"):
            raise GuardFailure(f"actor {actor!r} may not fire this edge (owner is {edge.get('by')!r})")
        _check_guards(conn, m, edge, task, ctx)

        # "@history" returns the task to wherever it was PARKED FROM (statechart history): a
        # deferred proposal goes back to proposed — never skipping the front gate — a deferred
        # build task back to ready, a design task back to design. Falls back to the edge's
        # `default` (or 'ready') for legacy rows parked before history existed.
        to = edge["to"]
        if to == "@history":
            parked = task["parked_from"] if "parked_from" in task.keys() else None
            to = parked if parked in (m or {}).get("states", {}) else edge.get("default", "ready")
        cas = conn.execute("UPDATE tasks SET status=?, updated_at=datetime('now') "
                           "WHERE id=? AND status=?", (to, tid, task["status"]))
        if cas.rowcount != 1:                       # someone else moved it between read and write
            raise GuardFailure(f"task {tid} left state {task['status']!r} concurrently — "
                               f"re-check with: dais edges {tid}")
        if (m or {}).get("states", {}).get(to, {}).get("history"):
            try:  # entering a history state — remember where from, so @history can return there
                conn.execute("UPDATE tasks SET parked_from=? WHERE id=?", (task["status"], tid))
            except sqlite3.OperationalError:
                pass                                # unmigrated db: @history just falls back
        # record the ACTUAL verb: run<->task attribution ('claim' marks pickup) AND the
        # dispatcher's no-progress throttle both read this — a fire IS progress, 'touch' is not.
        _link_run(conn, tid, verb)
        result = {"task": tid, "from": task["status"], "to": to, "verb": verb, "spawned": [], "encompassed": []}
        _apply_effect(conn, m, task, edge, result, ctx)
    except BaseException:
        if top:
            conn.execute("ROLLBACK TO SAVEPOINT dais_fire")
            conn.execute("RELEASE SAVEPOINT dais_fire")
        raise
    if top:
        conn.execute("RELEASE SAVEPOINT dais_fire")
        conn.commit()
    return result


def _link_run(conn, tid, verb):
    """Record run<->task in run_tasks when a run is active (DAIS_RUN_ID), mirroring the CLI. Best-
    effort: swallows a missing table (pre-`dais migrate`) so a transition never fails on the link."""
    rid = os.environ.get("DAIS_RUN_ID", "")
    if not rid.isdigit():
        return
    try:
        conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(?,?,?)", (int(rid), tid, verb))
    except sqlite3.Error:
        pass


def _apply_effect(conn, m, task, edge, result, ctx):
    eff = edge.get("effect", {})
    if "spawn" in eff:
        _effect_spawn(conn, task, eff["spawn"], result)
    if "aggregate" in eff:
        _effect_aggregate(conn, m, task, eff["aggregate"], result)
    if "then" in eff:
        _effect_then(conn, m, task, eff["then"], ctx)
    # eff["script"] would run an external effect script here (merge/deploy/publish) — out of scope
    # for the engine prototype; the transition + guards above are what make it safe.


def _effect_spawn(conn, task, sp, result):
    """spawn: create a linked child task (a fix, an impl from a proposal, a rollback)."""
    title = f"[{sp.get('template','task')}] {task['title']}"
    cid = _insert_task(conn, task["project"], title, sp["initial"], sp.get("by"))
    rel = {"from_proposal": "spawned_from", "blocks_parent": "blocks_parent",
           "part_of": "part_of"}.get(sp.get("rel"), "spawned_from")
    conn.execute("INSERT INTO task_links(parent_id,child_id,rel) VALUES(?,?,?)",
                 (task["id"], cid, rel))
    _link_run(conn, cid, "create")
    result["spawned"].append({"id": cid, "rel": rel, "state": sp["initial"]})


def _effect_aggregate(conn, m, task, agg, result):
    """aggregate: pull every task matching the select into this task's `encompasses` set."""
    sel = agg.get("select", "")
    want = dict(kv.split("=", 1) for kv in sel.split(",") if "=" in kv)
    rows = conn.execute("SELECT id FROM tasks WHERE project=? AND status=?",
                        (task["project"], want.get("state"))).fetchall()
    # "already encompassed" counts only links whose encompassing parent is still OPEN —
    # a task swept into a release that was later aborted/cancelled (terminal parent) is
    # re-aggregatable, else it would strand in the select state with no path out.
    terminals = sorted(s for s, meta in m.get("states", {}).items() if meta.get("terminal"))
    q = ("SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
         "WHERE l.child_id=? AND l.rel='encompasses'")
    if terminals:
        q += " AND p.status NOT IN (%s)" % ",".join("?" * len(terminals))
    for (cid,) in rows:
        if conn.execute(q, [cid] + terminals).fetchone():
            continue
        conn.execute("INSERT INTO task_links(parent_id,child_id,rel) VALUES(?,?,'encompasses')",
                     (task["id"], cid))
        result["encompassed"].append(cid)


def _effect_then(conn, m, task, spec, ctx):
    """then: fire an edge on related tasks (effects fire edges — never poke state).
    spec: "encompassed:<from>-><to>" — e.g. shipping a release closes everything it encompasses."""
    scope, _, transition = spec.partition(":")
    from_state, _, to_state = transition.partition("->")
    if scope != "encompassed":
        return
    kids = conn.execute("SELECT child_id FROM task_links WHERE parent_id=? AND rel='encompasses'",
                        (task["id"],)).fetchall()
    child_edge = next((e for e in m["edges"]
                       if e["from"] == from_state and e["to"] == to_state), None)
    for (cid,) in kids:
        c = task_row(conn, cid)
        if c and c["status"] == from_state and child_edge:
            # nested: joins the outer fire's transaction — the release and every
            # child close together, or the whole fire rolls back.
            fire(conn, m, cid, child_edge["verb"], "system", ctx, _nested=True)


def _system_edge_sweep(conn, m, project, match):
    """Fire every system edge selected by `match(edge)` on the project's tasks sitting in that
    edge's from-state. A task whose guards don't hold is skipped (not an error) so one held-back
    task never blocks the rest of the sweep. Returns the ids advanced."""
    moved = []
    for e in m.get("edges", []):
        if e.get("by") != "system" or not match(e):
            continue
        q, args = "SELECT id FROM tasks WHERE status=?", [e["from"]]
        if project:
            q += " AND project=?"; args.append(project)
        for (tid,) in conn.execute(q, args).fetchall():
            try:
                fire(conn, m, tid, e["verb"], "system")
                moved.append(tid)
            except GuardFailure:
                continue
    return moved


def advance_unblocked(conn, m, project=None):
    """Scheduler helper, run each dispatch tick: any task in a state with a system `unblocked` edge
    whose blockers are all terminal gets that edge fired (e.g. blocked -> qa_review once the spawned
    fix lands). Scoped to `project` so one machine never sweeps another project's same-named states."""
    return _system_edge_sweep(conn, m, project,
                              lambda e: "unblocked" in e.get("guards", []))


def recover_interrupted(conn, m, project):
    """Orphan-reconcile, run by the dispatcher only when NO agent is live: fire the machine's own
    system `interrupt` edge(s) so mid-flight tasks return to whatever state the machine says
    re-dispatches them — the machine decides where work resumes, never a hardcoded status.
    (`interrupt` is a harness event name, like the `unblocked` guard: authoring a
    `by: system, verb: interrupt` edge is how a machine opts into rewind-on-interruption.)"""
    return _system_edge_sweep(conn, m, project,
                              lambda e: e.get("verb") == "interrupt")


# --------------------------------------------------------------------------- #
# task creation (enters at an initial state)
# --------------------------------------------------------------------------- #
def create_task(conn, m, project, title, state=None, assignee=None,
                tid=None, priority=None, notes=None, blocked_on=None):
    """Create a task at `state` (default: the machine's entry). The one authority for what
    `dais task add` needs — state validation, id allocation (or an explicit tid), and the
    optional board attributes — so the CLI delegates here instead of re-deriving any of it.
    assignee: omitted -> the state's dispatch role (what spawns/python callers want);
    '' -> explicitly unassigned (what a founder's bare `task add` means)."""
    initials = [s for s, meta in m["states"].items() if meta.get("initial")]
    st = state or m.get("entry") or (initials[0] if initials else "backlog")
    if st not in m.get("states", {}):
        raise ValueError(f"invalid status '{st}'. valid: {' '.join(m.get('states', {}))}")
    if blocked_on:
        have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "blocked_on" not in have:                     # column arrives by migration 0001
            raise ValueError("dependencies need a schema upgrade — run 'dais migrate' first")
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (blocked_on,)).fetchone():
            raise ValueError(f"no such predecessor task: {blocked_on}")
    asg = assignee if assignee is not None else dispatch_role(m, st)
    extra = {k: v for k, v in (("priority", priority), ("notes", notes),
                               ("blocked_on", blocked_on)) if v is not None}
    tid = _insert_task(conn, project, title, st, asg or None, tid=tid, extra=extra)
    conn.commit()
    return tid


def open_db(path):
    """The one sqlite opening for the harness (rows by name, 10s busy timeout so concurrent
    writers wait for the lock instead of dying on 'database is locked' — the python twin of
    lib.sh's `.timeout 10000`). dashboard.connect delegates here."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# __main__ — the surface the bash `dais` CLI shells (like actions.py)
# --------------------------------------------------------------------------- #


def _main(argv):
    if not argv:
        print("usage: machine.py lint <machine> | edges <machine> <state> | "
              "create <db> <machine> <project> <title> [state] [--id X] [--priority P] "
              "[--assignee A] [--notes N] [--blocked-on ID] | "
              "fire <db> <machine> <task> <verb> <actor> [--confirm] [--typed X] "
              "[--attest fact] [--verify check] | "
              "advance <db> <machine> <project> | recover <db> <machine> <project>", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd in ("advance", "recover"):
        # dispatcher hooks: `advance` fires system `unblocked` edges each tick; `recover` fires
        # system `interrupt` edges on orphan-reconcile. Prints the ids moved, one per line.
        conn = open_db(argv[1])
        f = advance_unblocked if cmd == "advance" else recover_interrupted
        for tid in f(conn, load(argv[2]), argv[3]):
            print(tid)
        return 0
    if cmd == "lint":
        errors, warns = lint(load(argv[1]))
        for w in warns:
            print(f"  ⚠  {w}")
        for e in errors:
            print(f"  ✗  {e}")
        if not errors:
            print("  ✓ machine coherent" + (f" ({len(warns)} warning(s))" if warns else ""))
        return 1 if errors else 0
    if cmd == "edges":
        m = load(argv[1])
        for e in edges_from(m, argv[2]):
            g = (" {" + ",".join(e.get("guards", [])) + "}") if e.get("guards") else ""
            print(f"  {e['verb']:16} by {e['by']:9} -> {e['to']}{g}")
        d = dispatch_role(m, argv[2])
        print(f"  (dispatch: {d or '— parks/awaits'})")
        return 0
    if cmd == "create":
        # create <db> <machine> <project> <title> [state] [--id X] [--priority P]
        #        [--assignee A] [--notes N] [--blocked-on ID]
        # prints "<id>|<state>" (the CLI's `task add` echoes both); errors go to stderr, exit 1.
        db, mp, project, title = argv[1], argv[2], argv[3], argv[4]
        st, kw, rest = None, {}, argv[5:]
        flags = {"--id": "tid", "--priority": "priority", "--assignee": "assignee",
                 "--notes": "notes", "--blocked-on": "blocked_on"}
        i = 0
        while i < len(rest):
            if rest[i] in flags:
                kw[flags[rest[i]]] = rest[i + 1]; i += 2
            elif st is None and not rest[i].startswith("--"):
                st = rest[i]; i += 1
            else:
                i += 1
        conn = open_db(db)
        try:
            tid = create_task(conn, load(mp), project, title, st, **kw)
        except ValueError as ex:
            print(ex, file=sys.stderr); return 1
        print(f"{tid}|{conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0]}")
        return 0
    if cmd == "fire":
        db, mp, tid, verb, actor = argv[1:6]
        ctx, rest = {}, argv[6:]
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--confirm":
                ctx["confirm"] = True; i += 1
            elif a == "--typed":
                ctx["typed"] = rest[i + 1]; i += 2
            elif a == "--attest":
                ctx.setdefault("attest", {})[rest[i + 1]] = True; i += 2
            elif a == "--verify":
                ctx.setdefault("verifiers", {})[rest[i + 1]] = True; i += 2
            else:
                i += 1
        conn = open_db(db)
        try:
            r = fire(conn, load(mp), tid, verb, actor, ctx)
        except (GuardFailure, ValueError) as ex:
            print(f"  ✗ {ex}", file=sys.stderr); return 1
        extra = ""
        if r["spawned"]:
            extra += "  spawned " + ", ".join(f"{s['id']}({s['rel']}→{s['state']})" for s in r["spawned"])
        if r["encompassed"]:
            extra += "  encompassed " + ", ".join(r["encompassed"])
        print(f"  {r['task']}: {r['from']} --{r['verb']}--> {r['to']}{extra}")
        return 0
    print(f"machine.py: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
