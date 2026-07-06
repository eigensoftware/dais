"""dais board — the read side of dais.db and the project config around it.

The data layer under every renderer: the Task/Run/Project/Snapshot shapes,
`load_snapshot` (one coherent read of the whole workspace), run history, and
the per-project config readers (project.yaml fields, the authored machine).
Read-only: this module never writes dais.db.

dashboard.py (the renderers + the TUI action engine) builds on this and
re-exports these names, so `import dashboard as d` keeps working everywhere.
"""
import datetime as _dt
import os
import sqlite3
from dataclasses import dataclass, field

from actions import Action
import machine as MC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tool code dir
HOME = os.environ.get("DAIS_HOME") or ROOT                           # workspace (data) dir
DB = os.path.join(HOME, "dais.db")


# --------------------------------------------------------------------------- #
# time primitives (DB stamps are SQLite datetime('now') = UTC)
# --------------------------------------------------------------------------- #
def _parse(ts):
    try:
        return _dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def minutes_between(a, b):
    """Whole-minute gap between two 'YYYY-MM-DD HH:MM:SS' stamps, or None."""
    pa, pb = _parse(a), _parse(b)
    if pa is None or pb is None:
        return None
    return max(0, int((pb - pa).total_seconds() // 60))


def seconds_between(a, b):
    """Whole-second gap between two stamps, or None. Same UTC basis as the DB."""
    pa, pb = _parse(a), _parse(b)
    if pa is None or pb is None:
        return None
    return max(0, int((pb - pa).total_seconds()))


def utc_now():
    """DB-comparable 'now' — runs are stamped with SQLite datetime('now') = UTC, so
    elapsed/cap math MUST use UTC too (mixing in local time was the 'always 0m' bug)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# the shapes
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    id: str
    title: str
    status: str
    priority: str
    assignee: str = None
    pr_url: str = None
    notes: str = None
    updated_at: str = None   # last status change — used to sort the archive newest-first
    blocked_on: str = None   # predecessor task id this task waits on (dependency)
    blocked: bool = False     # computed: blocked_on is set AND that predecessor isn't done/cancelled


@dataclass
class Run:
    started_at: str
    agent: str
    status: str
    summary: str = None
    log_path: str = None
    dur_min: int = None
    project: str = None
    model: str = None         # the model the run launched with (runs.model, migration 0006); None pre-migration
    id: int = None            # runs.id — needed to join the authoritative run_tasks links
    task_ids: tuple = ()      # tasks this run touched (from run_tasks); () when unlinked/pre-migration
    claim: str = None         # the task this run picked up (verb='claim'), if any — else None


@dataclass
class Project:
    name: str
    stage_goal: str
    running: list = field(default_factory=list)
    tasks_by_status: dict = field(default_factory=dict)
    recent_runs: list = field(default_factory=list)
    machine: dict = None              # the project's authored state machine (or None = legacy status routing)


@dataclass
class Snapshot:
    projects: list
    recent_runs: list
    cap_state: bool
    ts: str
    workspace: str = None          # workspace identity (dais.yaml `workspace:`), or None
    links: list = field(default_factory=list)   # composition graph: (parent_id, child_id, rel)
                                                # rows from task_links; [] pre-migration


# --------------------------------------------------------------------------- #
# db + process probes
# --------------------------------------------------------------------------- #
def connect(db=DB):
    return MC.open_db(db)


def _has_column(conn, table, col):
    """True if `table` has column `col`. Lets readers degrade gracefully on a dais.db that hasn't had
    `dais migrate` run yet (e.g. blocked_on) instead of crashing the panel/scheduler on a missing col."""
    try:
        return any(r["name"] == col for r in conn.execute("PRAGMA table_info(%s)" % table))
    except sqlite3.Error:
        return False


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def running_agents(project_dir, is_alive=_pid_alive):
    out = []
    try:
        names = os.listdir(project_dir)
    except OSError:
        return out
    for n in names:
        if not n.startswith(".lock-"):
            continue
        agent = n[len(".lock-"):]
        try:
            with open(os.path.join(project_dir, n)) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        if is_alive(pid):
            out.append(agent)
    return sorted(out)


# --------------------------------------------------------------------------- #
# project config readers
# --------------------------------------------------------------------------- #
def project_field(root, name, key):
    """First-line value of `key:` from a project's project.yaml ('' if absent). Line-based, matching
    the bash `pcfg` reader — used for stage_goal, deploy, etc."""
    path = os.path.join(root, "projects", name, "project.yaml")
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def agent_model(root, project, agent):
    """The (model, effort) a run will actually use — reads through router.agent_setup, THE
    resolution authority (frontmatter -> legacy roles file -> project.yaml -> defaults), so
    the panel can't drift from what run-agent.sh resolves. (The old body re-implemented the
    pre-frontmatter project.yaml scheme and showed the stale default once a role's .md
    frontmatter overrode `model:`.)"""
    import router
    s = router.agent_setup(root, project, agent)
    return s["model"], s["effort"]


def stage_goal(root, name):
    return project_field(root, name, "stage_goal")


def _load_machine(root, name):
    """The project's authored state machine (dict) — ALWAYS one, so the whole TUI is machine-driven
    like dispatch: the project's own machine.json → a `machine:` selector → the coding default.
    Returns None only if the resolved machine file can't be loaded at all."""
    ref = project_field(root, name, "machine")
    try:
        return MC.load(MC.project_machine_path(root, name, ref))
    except Exception:
        return None


_REVERSE_VERBS = {"reject", "cancel", "request_changes", "abort", "give_up", "defer"}


def _machine_actions(m, state):
    """Action rows for a machine task, derived from its outgoing edges (MC.edge_actions). The founder
    sees: `start` (launch the dispatch agent, if any) as advance, one founder edge as advance ('a'),
    one reverse-ish founder edge as reverse ('x'), the rest under the Enter menu. Agent-only edges
    (claim/complete/pass…) aren't founder keys — they fire from agent runs. `confirm` carries through
    so guarded edges prompt; strong-human guards (typed_confirm/attest) are handled at execution."""
    acts, adv_used, rev_used = [], False, False
    for a in MC.edge_actions(m, state):
        verb = a["verb"]
        if verb == "__start":
            acts.append(Action("__start", a["label"], "a", "advance", False)); adv_used = True
            continue
        if not a.get("human"):            # agent edges aren't the founder's to press
            continue
        is_rev = verb in _REVERSE_VERBS
        if is_rev and not rev_used:
            key, slot, rev_used = "x", "reverse", True
        elif not is_rev and not adv_used:
            key, slot, adv_used = "a", "advance", True
        else:
            key, slot = "", "menu"
        acts.append(Action(verb, a["label"], key, slot, a["confirm"]))
    if not (m or {}).get("states", {}).get(state, {}).get("terminal"):
        acts.append(Action("set_priority", "set priority", "", "menu", False))   # no-op on a terminal task
    acts.append(Action("edit_title", "edit title", "e", "menu", False))          # metadata, orthogonal
    return acts


def workspace_name(home=HOME):
    """The `workspace:` value from the workspace's dais.yaml (line-based, mirroring
    stage_goal), or None when the file or key is absent / the value is empty."""
    path = os.path.join(home, "dais.yaml")
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("workspace:"):
                    return line.split(":", 1)[1].strip() or None
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- #
# the snapshot — one coherent read of the whole workspace
# --------------------------------------------------------------------------- #
_PRIO = ("CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
         "WHEN 'medium' THEN 2 ELSE 3 END")


def load_snapshot(conn, root=HOME, now=None, recent=6):
    now = now or utc_now()
    projects = []
    dep = ",blocked_on" if _has_column(conn, "tasks", "blocked_on") else ""
    mcol = ",model" if _has_column(conn, "runs", "model") else ""    # migration 0006
    # Projects to render = those configured on disk (a dir under projects/ with a project.yaml —
    # the marker lint requires; the roles file is legacy and optional) UNIONed with any project
    # referenced by a task. The union keeps a configured-but-taskless project visible in the
    # panel, and still surfaces an "orphaned" project whose directory was removed but whose tasks
    # linger on the board.
    pdir = os.path.join(root, "projects")
    on_disk = ([d for d in os.listdir(pdir)
                if os.path.exists(os.path.join(pdir, d, "project.yaml"))]
               if os.path.isdir(pdir) else [])
    tasked = [r["project"] for r in conn.execute("SELECT DISTINCT project FROM tasks")]
    names = sorted(set(on_disk) | set(tasked))
    for name in names:
        rows = conn.execute(
            "SELECT id,title,status,priority,assignee,pr_url,notes,updated_at" + dep + " FROM tasks "
            "WHERE project=? ORDER BY " + _PRIO + ", id", (name,)).fetchall()
        by_status = {}
        for r in rows:
            by_status.setdefault(r["status"], []).append(Task(
                id=r["id"], title=r["title"], status=r["status"],
                priority=r["priority"], assignee=r["assignee"],
                pr_url=r["pr_url"], notes=r["notes"], updated_at=r["updated_at"],
                blocked_on=(r["blocked_on"] if dep else None)))
        run_rows = conn.execute(
            "SELECT id,started_at,ended_at,agent,status,summary,log_path" + mcol + " FROM runs "
            "WHERE project=? ORDER BY id DESC LIMIT ?", (name, recent)).fetchall()
        proj_runs = [Run(id=r["id"], started_at=r["started_at"], agent=r["agent"],
                         status=r["status"], summary=r["summary"],
                         log_path=r["log_path"], project=name,
                         model=(r["model"] if mcol else None),
                         dur_min=minutes_between(r["started_at"], r["ended_at"]))
                     for r in run_rows]
        attach_run_tasks(conn, proj_runs)
        running = []
        for agent in running_agents(os.path.join(root, "projects", name)):
            since = conn.execute(
                "SELECT started_at FROM runs WHERE project=? AND agent=? "
                "AND status='running' ORDER BY id DESC LIMIT 1",
                (name, agent)).fetchone()
            running.append((agent, since["started_at"] if since else None))
        projects.append(Project(name=name, stage_goal=stage_goal(root, name),
                                running=running, tasks_by_status=by_status,
                                recent_runs=proj_runs,
                                machine=_load_machine(root, name)))
    # resolve dependencies once across ALL projects (a predecessor may live in another project):
    # a task is blocked when its predecessor exists and isn't done/cancelled. A dangling ref
    # (predecessor missing) is treated as unblocked so a deleted prerequisite never strands work.
    status_by_id = {t.id: t.status for p in projects
                    for ts in p.tasks_by_status.values() for t in ts}
    for p in projects:
        for ts in p.tasks_by_status.values():
            for t in ts:
                t.blocked = bool(t.blocked_on) and \
                    status_by_id.get(t.blocked_on) not in (None, "done", "cancelled")
    grows = conn.execute(
        "SELECT id,started_at,ended_at,project,agent,status,summary,log_path" + mcol + " FROM runs "
        "ORDER BY id DESC LIMIT ?", (recent,)).fetchall()
    recent_runs = [Run(id=r["id"], started_at=r["started_at"],
                       agent=f"{r['project']}/{r['agent']}",
                       status=r["status"], summary=r["summary"],
                       log_path=r["log_path"], project=r["project"],
                       model=(r["model"] if mcol else None),
                       dur_min=minutes_between(r["started_at"], r["ended_at"]))
                   for r in grows]
    attach_run_tasks(conn, recent_runs)
    capped = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE status='capped' "
        "AND started_at > datetime(?, '-90 minutes')", (now,)).fetchone()["c"]
    try:                        # composition graph (task_links, migration 0003); [] pre-migration
        links = [(r["parent_id"], r["child_id"], r["rel"]) for r in
                 conn.execute("SELECT parent_id, child_id, rel FROM task_links ORDER BY id")]
    except sqlite3.Error:
        links = []
    return Snapshot(projects=projects, recent_runs=recent_runs,
                    cap_state=capped > 0, ts=now, workspace=workspace_name(root),
                    links=links)


def load_runs(conn, limit=200):
    """Org-wide run history (newest first) for the RUNS view — the full record, deeper than the
    snapshot's small FEED slice. Includes task-LESS runs (e.g. a lead planning pass) so completed
    work doesn't just flash by in the ticker and vanish."""
    mcol = ",model" if _has_column(conn, "runs", "model") else ""    # migration 0006
    rows = conn.execute(
        "SELECT started_at,ended_at,project,agent,status,summary,log_path" + mcol + " FROM runs "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [Run(started_at=r["started_at"],
                agent=f"{r['project']}/{r['agent']}",
                status=r["status"], summary=r["summary"],
                log_path=r["log_path"], project=r["project"],
                model=(r["model"] if mcol else None),
                dur_min=minutes_between(r["started_at"], r["ended_at"]))
            for r in rows]


def attach_run_tasks(conn, runs):
    """Populate each Run's authoritative task links from run_tasks (migration 0002): `.task_ids` (all
    tasks the run touched, in first-seen order) and `.claim` (the verb='claim' task it picked up, if
    any). A no-op that leaves the defaults when the run_tasks table is absent (a dais.db that predates
    `dais migrate`) or the runs carry no id — callers then fall back to the legacy summary scan."""
    ids = [r.id for r in runs if getattr(r, "id", None) is not None]
    if not ids:
        return runs
    try:
        rows = conn.execute(
            "SELECT run_id, task_id, verb FROM run_tasks WHERE run_id IN (%s) ORDER BY id"
            % ",".join("?" * len(ids)), ids).fetchall()
    except Exception:
        return runs                       # no run_tasks table yet -> summary-scan fallback
    touched, claim = {}, {}
    for row in rows:
        rid, tid, verb = row["run_id"], row["task_id"], row["verb"]
        lst = touched.setdefault(rid, [])
        if tid not in lst:
            lst.append(tid)
        if verb == "claim" and rid not in claim:
            claim[rid] = tid
    for r in runs:
        if r.id in touched:
            r.task_ids = tuple(touched[r.id])
            r.claim = claim.get(r.id)
    return runs


def runs_touching(runs, task_id):
    """Runs that touched `task_id`. Prefers the authoritative run_tasks links; falls back to the
    legacy summary substring-scan only for runs with no links (pre-migration history)."""
    return [r for r in runs
            if task_id in r.task_ids
            or (not r.task_ids and r.summary and task_id in r.summary)]
