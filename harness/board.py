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
import re
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
    updated_at: str = None   # last CHANGE of any kind (notes, priority, title, status) — archive sort
    state_entered_at: str = None  # when it entered its current state (0011); None pre-migration
    blocked_on: str = None   # predecessor task id this task waits on (dependency)
    blocked: bool = False     # computed: blocked_on is set AND that predecessor isn't done/cancelled
    blocked_status: str = None  # computed: the open predecessor's CURRENT state (shown on the row)
    over_budget: dict = None  # computed (plan 1.6): {'runs', 'tokens'} when the task passed its
                              # project's spend ceiling — the dispatcher withholds it until
                              # `dais task set <id> --budget-lift`


def entered_at(t):
    """When a task entered its current state — the stamp fire() writes (plan 2.3), falling back
    to updated_at for rows that predate it. THE reading for gate age everywhere (row tags, the
    vitals alarm, the inspector's "since")."""
    return getattr(t, "state_entered_at", None) or t.updated_at


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
    task_id: str = None       # the task PINNED at dispatch (runs.task_id) — the trigger, set before
                              # any claim; the RUNNING view prefers it over a re-derived guess


@dataclass
class Project:
    name: str
    stage_goal: str
    running: list = field(default_factory=list)
    tasks_by_status: dict = field(default_factory=dict)
    recent_runs: list = field(default_factory=list)
    machine: dict = None              # the project's authored state machine (or None = legacy status routing)
    last_tick: dict = None            # why the last tick left this project idle (plan 1.7), or None


# --- "why idle" (plan 1.7): the tick journal (projects/.watch.log) says why a tick launched
# nothing — throttle, stall, provider cooling, idle-check, budget, pause — and the board never
# showed it. Attribute each line to a project by the tokens dispatch.sh writes (`proj/role`,
# `[proj]`, `for proj:`); keep the newest per project and the newest workspace-level line. ---
def tick_journal(root, project_names, now_local=None, limit=400):
    import time as _time
    path = os.path.join(root, "projects", ".watch.log")
    out = {"workspace": None, "projects": {}}
    try:
        with open(path) as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return out
    now_local = now_local or _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        now_t = _time.mktime(_time.strptime(now_local, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        now_t = _time.time()

    def entry(ts, text):
        try:
            age = max(0, int((now_t - _time.mktime(_time.strptime(ts, "%Y-%m-%d %H:%M:%S"))) // 60))
        except ValueError:
            age = 0
        return {"ts": ts, "text": text, "age_min": age}

    for raw in lines:
        raw = raw.rstrip("\n")
        if not raw.startswith("[") or "] " not in raw:
            continue
        ts, text = raw[1:].split("] ", 1)
        owner = next((n for n in project_names
                      if (" %s/" % n) in text or ("[%s]" % n) in text or ("for %s:" % n) in text), None)
        if owner:
            out["projects"][owner] = entry(ts, text)      # newest wins (lines are in order)
        elif "/" not in text.split(" — ")[0]:            # a workspace-level line (no proj/role token)
            out["workspace"] = entry(ts, text)
    return out


def tick_reason_line(e):
    """One human line for a journal entry: the reason, how long ago, and when it retries when
    the reason implies a clock (throttle 45m, cap cooldown 90m, error backoff 30m)."""
    age = e.get("age_min", 0)
    ago = ("%dm" % age) if age < 60 else ("%dh%02dm" % (age // 60, age % 60))
    text = e["text"]
    head = text.lower()
    hint = ""
    for key, mins in (("throttle", 45), ("cooling —", 90), ("backing off", 30)):
        if head.startswith(key):
            left = mins - age
            hint = (", retry ≈%dm" % left) if left > 0 else ", retry due"
            break
    if head.startswith("idle-check"):
        hint = ", wakes on a board change"
    elif head.startswith("daily budget"):
        hint = ", resumes tomorrow"
    return "%s (%s ago%s)" % (text if len(text) <= 90 else text[:88] + "…", ago, hint)


@dataclass
class Snapshot:
    projects: list
    recent_runs: list
    cap_state: bool                # any provider cooling (= bool(cooling)); kept for callers
    ts: str
    workspace: str = None          # workspace identity (dais.yaml `workspace:`), or None
    cooling: list = field(default_factory=list)  # providers the dispatcher's cap gate is holding
                                                 # (['all'] on a db without runs.provider)
    budget: dict = None            # the workspace daily budget (plan 1.6): {'limit','unit','spent',
                                   # 'over'}, or None when none is set — mirrors the dispatcher
    last_tick: dict = None         # the newest tick-journal entry anywhere (plan 1.7), or None
    links: list = field(default_factory=list)   # composition graph: (parent_id, child_id, rel)
                                                # rows from task_links; [] pre-migration
    archived: list = field(default_factory=list)  # projects hidden from the board (`archived: true`
                                                  # in project.yaml) — data stays in the db


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
        base, dot, tail = agent.rpartition(".")
        if dot and tail.isdigit():
            agent = base           # slot suffix (.2..) from role concurrency — same role
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
def project_archived(root, name):
    """True when the project opted off the board (`archived: true` in project.yaml). Archiving
    hides — it never deletes: tasks/runs/notes stay in the db, `dais project|tasks <name>` still
    answer by name, and `dais unarchive` restores. The flag must be POSITIVE (not a moved dir):
    load_snapshot's disk∪tasks union would resurface a moved project from its lingering tasks."""
    return project_field(root, name, "archived").lower() in ("true", "yes", "1")


# A YAML block-scalar indicator (folded `>`/`>-`/`>+` or literal `|`/`|-`/`|+`) as the WHOLE
# value after `key:` — the reader must fold in the following more-indented lines instead of
# returning the two-character indicator literally.
_BLOCK_SCALAR = re.compile(r"^[|>][+-]?[ \t]*$")


def project_field(root, name, key):
    """First-line value of `key:` from a project's project.yaml ('' if absent). Line-based, matching
    the bash `pcfg` reader — used for stage_goal, deploy, etc. A block-scalar value (`key: >-` etc.)
    is folded: every following MORE-INDENTED line is joined with spaces (good enough for these
    single-paragraph fields — a real incident: `stage_goal: >-` reached an agent's prompt as the
    literal string '>-' because this reader, being line-based, never followed the fold)."""
    path = os.path.join(root, "projects", name, "project.yaml")
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    for i, line in enumerate(lines):
        if line.startswith(key + ":"):
            v = line.split(":", 1)[1].strip()
            if not _BLOCK_SCALAR.match(v):
                return v
            out = []
            for cont in lines[i + 1:]:
                if re.match(r"^[ \t]+\S", cont):
                    out.append(cont.strip())
                else:
                    break
            return " ".join(out)
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


def agent_provider(root, project, agent):
    """The provider a run will actually use (anthropic | openai | …), through the same
    authority as agent_model — so a role switched to codex reads as such everywhere."""
    import router
    return router.agent_setup(root, project, agent)["provider"]


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
    acts.append(Action("add_note", "add note", "N", "menu", False))              # appends to the notes log
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


def load_snapshot(conn, root=HOME, now=None, recent=6, now_local=None):
    now = now or utc_now()
    projects = []
    dep = ",blocked_on" if _has_column(conn, "tasks", "blocked_on") else ""
    sea = ",state_entered_at" if _has_column(conn, "tasks", "state_entered_at") else ""
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
    # archived projects (project.yaml `archived: true`) leave the board entirely — rail, WORK,
    # status — but their names ride the snapshot so renderers can say what's hidden.
    archived = [n for n in names if project_archived(root, n)]
    names = [n for n in names if n not in archived]
    for name in names:
        rows = conn.execute(
            "SELECT id,title,status,priority,assignee,pr_url,notes,updated_at" + dep + sea + " FROM tasks "
            "WHERE project=? ORDER BY " + _PRIO + ", id", (name,)).fetchall()
        by_status = {}
        for r in rows:
            by_status.setdefault(r["status"], []).append(Task(
                id=r["id"], title=r["title"], status=r["status"],
                priority=r["priority"], assignee=r["assignee"],
                pr_url=r["pr_url"], notes=r["notes"], updated_at=r["updated_at"],
                state_entered_at=(r["state_entered_at"] if sea else None),
                blocked_on=(r["blocked_on"] if dep else None)))
        run_rows = conn.execute(
            "SELECT id,started_at,ended_at,agent,status,summary,log_path,task_id" + mcol + " FROM runs "
            "WHERE project=? ORDER BY id DESC LIMIT ?", (name, recent)).fetchall()
        proj_runs = [Run(id=r["id"], started_at=r["started_at"], agent=r["agent"],
                         status=r["status"], summary=r["summary"],
                         log_path=r["log_path"], project=name, task_id=r["task_id"],
                         model=(r["model"] if mcol else None),
                         dur_min=minutes_between(r["started_at"], r["ended_at"]))
                     for r in run_rows]
        attach_run_tasks(conn, proj_runs)
        # running: one (agent, since, run_id) triple PER LIVE LOCK SLOT — same-role concurrency
        # (.lock-writer + .lock-writer.2) yields the same agent name twice, so slots pair with
        # their OWN run by fetching that agent's running runs ONCE (oldest first) and assigning
        # run i to slot i, instead of re-querying "the newest running run" per slot (which
        # collapsed every slot of a stacked role onto the same run/timestamp/task).
        running = []
        agent_runs = {}    # agent -> [ {id, started_at}, ... ] oldest first, consumed by index
        agent_slot = {}    # agent -> next unconsumed index into agent_runs[agent]
        for agent in running_agents(os.path.join(root, "projects", name)):
            if agent not in agent_runs:
                agent_runs[agent] = conn.execute(
                    "SELECT id,started_at FROM runs WHERE project=? AND agent=? "
                    "AND status='running' ORDER BY id",
                    (name, agent)).fetchall()
                agent_slot[agent] = 0
            i = agent_slot[agent]
            row = agent_runs[agent][i] if i < len(agent_runs[agent]) else None
            agent_slot[agent] = i + 1
            running.append((agent, row["started_at"] if row else None,
                            row["id"] if row else None))
        projects.append(Project(name=name, stage_goal=stage_goal(root, name),
                                running=running, tasks_by_status=by_status,
                                recent_runs=proj_runs,
                                machine=_load_machine(root, name)))
    # resolve dependencies once across ALL projects (a predecessor may live in another project):
    # a task is blocked when its predecessor exists and isn't done/cancelled. A dangling ref
    # (predecessor missing) is treated as unblocked so a deleted prerequisite never strands work.
    status_by_id = {t.id: t.status for p in projects
                    for ts in p.tasks_by_status.values() for t in ts}
    import router
    for p in projects:
        # spend ceiling (plan 1.6): the same set the dispatcher withholds, on the same conn
        over = router.over_budget_tasks(root, p.name, conn=conn)
        for ts in p.tasks_by_status.values():
            for t in ts:
                t.blocked = bool(t.blocked_on) and \
                    status_by_id.get(t.blocked_on) not in (None, "done", "cancelled")
                t.blocked_status = status_by_id.get(t.blocked_on) if t.blocked else None
                t.over_budget = over.get(t.id)
    budget = router.daily_budget_state(root, now=now, conn=conn)
    # "why idle" (plan 1.7): the tick journal, newest entry per project + the newest overall
    journal = tick_journal(root, [p.name for p in projects], now_local=now_local)
    newest = None
    for p in projects:
        p.last_tick = journal["projects"].get(p.name)
        if p.last_tick and (newest is None or p.last_tick["ts"] > newest["ts"]):
            newest = p.last_tick
    if journal["workspace"] and (newest is None or journal["workspace"]["ts"] > newest["ts"]):
        newest = journal["workspace"]
    grows = conn.execute(
        "SELECT id,started_at,ended_at,project,agent,status,summary,log_path,task_id" + mcol + " FROM runs "
        "ORDER BY id DESC LIMIT ?", (recent,)).fetchall()
    recent_runs = [Run(id=r["id"], started_at=r["started_at"],
                       agent=f"{r['project']}/{r['agent']}",
                       status=r["status"], summary=r["summary"],
                       log_path=r["log_path"], project=r["project"], task_id=r["task_id"],
                       model=(r["model"] if mcol else None),
                       dur_min=minutes_between(r["started_at"], r["ended_at"]))
                   for r in grows]
    attach_run_tasks(conn, recent_runs)
    # Mirror the dispatcher's cap gate (dispatch.sh): a success AFTER the last cap proves the
    # window is back, so only count caps newer than the latest success. Without this the badge
    # shows COOLING for the full 90m even after the loop has already resumed dispatching.
    # Per PROVIDER (runs.provider, 0007), exactly like the gate: a cap counts only against the
    # provider that hit it, and only that provider's later success clears it. NULL = anthropic.
    try:
        cooling = [r["p"] for r in conn.execute(
            "SELECT DISTINCT COALESCE(r.provider,'anthropic') p FROM runs r WHERE r.status='capped' "
            "AND r.started_at > datetime(?, '-90 minutes') "
            "AND r.started_at > COALESCE((SELECT MAX(s.started_at) FROM runs s "
            "WHERE s.status='succeeded' AND COALESCE(s.provider,'anthropic')="
            "COALESCE(r.provider,'anthropic')), '') ORDER BY p", (now,))]
    except sqlite3.OperationalError:            # pre-0007 db: providers indistinguishable
        capped = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE status='capped' "
            "AND started_at > datetime(?, '-90 minutes') "
            "AND started_at > COALESCE((SELECT MAX(started_at) FROM runs "
            "WHERE status='succeeded'), '')", (now,)).fetchone()["c"]
        cooling = ["all"] if capped else []
    try:                        # composition graph (task_links, migration 0003); [] pre-migration
        links = [(r["parent_id"], r["child_id"], r["rel"]) for r in
                 conn.execute("SELECT parent_id, child_id, rel FROM task_links ORDER BY id")]
    except sqlite3.Error:
        links = []
    return Snapshot(projects=projects, recent_runs=recent_runs,
                    cap_state=bool(cooling), cooling=cooling, budget=budget, last_tick=newest, ts=now,
                    workspace=workspace_name(root), links=links, archived=archived)


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
