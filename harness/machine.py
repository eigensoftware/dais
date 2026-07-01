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
    """Return (errors, warnings). Errors mean the machine is malformed / non-deterministic."""
    errors, warns = [], []
    states = m.get("states", {})
    roles = set(m.get("roles", {})) | IMPLICIT_ACTORS
    edges = m.get("edges", [])
    terminals = {s for s, meta in states.items() if meta.get("terminal")}
    initials = {s for s, meta in states.items() if meta.get("initial")}
    out = {s: [] for s in states}

    for i, e in enumerate(edges):
        tag = f"edge[{i}] {e.get('from','?')}--{e.get('verb','?')}-->{e.get('to','?')}"
        if e.get("from") in states:
            out[e["from"]].append(e)
        else:
            errors.append(f"E1 {tag}: unknown `from` state {e.get('from')!r}")
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

    for s in states:
        if s not in terminals and not out.get(s):
            errors.append(f"E2 dead-end: non-terminal state {s!r} has no outgoing edge")

    for s in states:
        if states[s].get("pool"):
            continue
        agents = {e.get("by") for e in out.get(s, [])
                  if e.get("by") and e.get("by") not in IMPLICIT_ACTORS}
        if len(agents) > 1:
            errors.append(f"E3 ambiguous dispatch: state {s!r} auto-dispatches multiple roles "
                          f"{sorted(agents)} (add \"pool\": true to allow any-of)")

    if not initials:
        errors.append("E4: no `initial` state declared")
    if not terminals:
        errors.append("E4: no `terminal` state declared")
    if m.get("entry") and m["entry"] not in states:
        errors.append(f"E4: `entry` {m['entry']!r} is not a state (new tasks would enter an "
                      f"unknown state with no edges)")

    # E5: a duplicate (from, verb) pair is non-deterministic — fire() takes the first match,
    # silently shadowing the other edge (and whatever guards/effects it carries).
    seen_fv = {}
    for e in edges:
        fv = (e.get("from"), e.get("verb"))
        if all(fv) and fv in seen_fv:
            errors.append(f"E5 duplicate edge: {fv[0]!r} --{fv[1]}--> appears more than once "
                          f"(-> {seen_fv[fv]!r} and -> {e.get('to')!r}); the second is unreachable")
        elif all(fv):
            seen_fv[fv] = e.get("to")

    # entry points: declared initials PLUS any state a spawn effect drops a new task into
    spawn_targets = {e["effect"]["spawn"]["initial"] for e in edges
                     if e.get("effect", {}).get("spawn", {}).get("initial") in states}
    roots = set(initials) | spawn_targets
    seen, stack = set(roots), list(roots)
    while stack:
        for e in out.get(stack.pop(), []):
            if e["to"] in states and e["to"] not in seen:
                seen.add(e["to"]); stack.append(e["to"])
    for s in states:
        if s not in seen:
            warns.append(f"W1 unreachable: {s!r} not reachable from an initial state or a spawn")

    preds = {s: [] for s in states}
    for e in edges:
        if e.get("from") in states and e.get("to") in states:
            preds[e["to"]].append(e["from"])
    can_end, stack = set(terminals), list(terminals)
    while stack:
        for p in preds.get(stack.pop(), []):
            if p not in can_end:
                can_end.add(p); stack.append(p)
    for s in states:
        if s not in can_end:
            warns.append(f"W2 no exit: {s!r} cannot reach any terminal state")

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
    return errors, warns


# --------------------------------------------------------------------------- #
# engine — introspection
# --------------------------------------------------------------------------- #
def edges_from(m, state):
    return [e for e in (m or {}).get("edges", []) if e.get("from") == state]


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


def next_role(conn, m, project):
    """Reactive dispatch (the scheduler): the role to launch next for this project — the dispatch
    role of the highest-priority pending task sitting in a state that auto-dispatches. '' when
    nothing is dispatchable (the caller then considers cadence roles). Blocked/parked/gate states
    have no dispatch role, so they're naturally skipped; a task waiting on an open dependency
    (tasks.blocked_on) is skipped too."""
    rows = conn.execute("SELECT id, status, COALESCE(priority,'medium') FROM tasks "
                        "WHERE project=? AND status NOT IN ('done','cancelled')", (project,)).fetchall()
    best = None
    for r in rows:
        rid = r["id"] if hasattr(r, "keys") else r[0]
        status = r["status"] if hasattr(r, "keys") else r[1]
        role = dispatch_role(m, status)
        if not role or _dep_open(conn, rid):
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


def _task(conn, tid):
    return conn.execute("SELECT id,project,status,title,assignee FROM tasks WHERE id=?", (tid,)).fetchone()


def _new_id(conn, project):
    abbr = project[:3]
    n = conn.execute("SELECT COUNT(*)+1 FROM tasks WHERE project=?", (project,)).fetchone()[0]
    tid = f"{abbr}-{n}"
    while conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        n += 1; tid = f"{abbr}-{n}"
    return tid


def _insert_task(conn, project, title, status, assignee):
    """INSERT with a fresh auto-id, retrying on an id collision — two concurrent creators
    (parallel agents adding tasks, racing spawn effects) compute the same next id; the loser
    re-derives instead of dying on the UNIQUE constraint. Returns the id used."""
    for _ in range(20):
        tid = _new_id(conn, project)
        try:
            conn.execute("INSERT INTO tasks(id,project,title,status,assignee) VALUES(?,?,?,?,?)",
                         (tid, project, title, status, assignee))
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
            fact = g[len("attest:"):].split(" ")[0]
            if not (ctx.get("attest") or {}).get(fact):
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
        task = _task(conn, tid)
        if not task:
            raise ValueError(f"no such task: {tid}")
        edge = _find_edge(m, task["status"], verb)
        if not edge:
            raise ValueError(f"no edge {verb!r} from state {task['status']!r} for task {tid}")
        if actor != edge.get("by"):
            raise GuardFailure(f"actor {actor!r} may not fire this edge (owner is {edge.get('by')!r})")
        _check_guards(conn, m, edge, task, ctx)

        cas = conn.execute("UPDATE tasks SET status=?, updated_at=datetime('now') "
                           "WHERE id=? AND status=?", (edge["to"], tid, task["status"]))
        if cas.rowcount != 1:                       # someone else moved it between read and write
            raise GuardFailure(f"task {tid} left state {task['status']!r} concurrently — "
                               f"re-check with: dais edges {tid}")
        _link_run(conn, tid, "claim" if verb == "claim" else "touch")
        result = {"task": tid, "from": task["status"], "to": edge["to"], "verb": verb, "spawned": [], "encompassed": []}
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
        sp = eff["spawn"]
        title = f"[{sp.get('template','task')}] {task['title']}"
        cid = _insert_task(conn, task["project"], title, sp["initial"], sp.get("by"))
        rel = {"from_proposal": "spawned_from", "blocks_parent": "blocks_parent",
               "part_of": "part_of"}.get(sp.get("rel"), "spawned_from")
        conn.execute("INSERT INTO task_links(parent_id,child_id,rel) VALUES(?,?,?)",
                     (task["id"], cid, rel))
        _link_run(conn, cid, "create")
        result["spawned"].append({"id": cid, "rel": rel, "state": sp["initial"]})
    if "aggregate" in eff:
        sel = eff["aggregate"].get("select", "")
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
    if "then" in eff:                     # fire an edge on related tasks (effects fire edges)
        scope, _, transition = eff["then"].partition(":")
        from_state, _, to_state = transition.partition("->")
        if scope == "encompassed":
            kids = conn.execute("SELECT child_id FROM task_links WHERE parent_id=? AND rel='encompasses'",
                                (task["id"],)).fetchall()
            child_edge = next((e for e in m["edges"]
                               if e["from"] == from_state and e["to"] == to_state), None)
            for (cid,) in kids:
                c = _task(conn, cid)
                if c and c["status"] == from_state and child_edge:
                    # nested: joins the outer fire's transaction — the release and every
                    # child close together, or the whole fire rolls back.
                    fire(conn, m, cid, child_edge["verb"], "system", ctx, _nested=True)
    # eff["script"] would run an external effect script here (merge/deploy/publish) — out of scope
    # for the engine prototype; the transition + guards above are what make it safe.


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
def create_task(conn, m, project, title, state=None, assignee=None):
    initials = [s for s, meta in m["states"].items() if meta.get("initial")]
    st = state or m.get("entry") or (initials[0] if initials else "backlog")
    tid = _insert_task(conn, project, title, st, assignee or dispatch_role(m, st))
    conn.commit()
    return tid


# --------------------------------------------------------------------------- #
# __main__ — the surface the bash `dais` CLI shells (like actions.py)
# --------------------------------------------------------------------------- #
def _open(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _main(argv):
    if not argv:
        print("usage: machine.py lint <machine> | edges <machine> <state> | "
              "create <db> <machine> <project> <title> [state] | "
              "fire <db> <machine> <task> <verb> <actor> [--confirm] [--typed X] "
              "[--attest fact] [--verify check] | "
              "advance <db> <machine> <project> | recover <db> <machine> <project>", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd in ("advance", "recover"):
        # dispatcher hooks: `advance` fires system `unblocked` edges each tick; `recover` fires
        # system `interrupt` edges on orphan-reconcile. Prints the ids moved, one per line.
        conn = _open(argv[1])
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
        db, mp, project, title = argv[1], argv[2], argv[3], argv[4]
        st = argv[5] if len(argv) > 5 else None
        conn = _open(db)
        print(create_task(conn, load(mp), project, title, st))
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
        conn = _open(db)
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
