#!/usr/bin/env python3
"""dais dashboard — shared data layer + two renderers.

Powers `dais status` (plain one-shot) and `dais top` (curses TUI).
Read-only: this module never writes dais.db.
"""
import curses
import datetime as _dt
import os
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
import unicodedata
from dataclasses import dataclass, field

# the action engine lives beside this file in harness/; `dais top` runs this module
# as a script (so harness/ is sys.path[0]) and the tests insert harness/ on the path,
# so a bare `from actions import …` resolves in both.
from actions import priority_cycle, Action  # noqa: F401
import router  # parse_roles — so the running-task guess can be agent-aware (which statuses a role owns)
import machine as MC  # machine-driven projects: derive the action bar from edges (not the actions.py catalog)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tool code dir
HOME = os.environ.get("DAIS_HOME") or ROOT                           # workspace (data) dir
DB = os.path.join(HOME, "dais.db")


# --------------------------------------------------------------------------- #
# formatting primitives
# --------------------------------------------------------------------------- #
def truncate_words(s, width):
    """Truncate `s` to <= width chars, breaking on a word boundary, with an ellipsis."""
    s = (s or "").strip()
    if len(s) <= width:
        return s
    if width <= 1:
        return "…"
    cut = s[:width - 1]
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def collapse_ids(ids, limit=8, sep=", "):
    """Join ids; past `limit`, show the first `limit` then ' (+N more)'."""
    ids = list(ids)
    if len(ids) <= limit:
        return sep.join(ids)
    return sep.join(ids[:limit]) + f" (+{len(ids) - limit} more)"


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


def fmt_elapsed(secs):
    """Compact live elapsed: '' / '45s' / '12:03' (m:ss) / '1h02m'."""
    if secs is None:
        return ""
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}:{secs % 60:02d}"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def utc_now():
    """DB-comparable 'now' — runs are stamped with SQLite datetime('now') = UTC, so
    elapsed/cap math MUST use UTC too (mixing in local time was the 'always 0m' bug)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def to_local_hhmm(utc_ts, with_secs=False):
    """A UTC DB stamp -> local clock string for display (HH:MM or HH:MM:SS)."""
    pa = _parse(utc_ts)
    if pa is None:
        return "--:--:--" if with_secs else "--:--"
    local = pa.replace(tzinfo=_dt.timezone.utc).astimezone()
    return local.strftime("%H:%M:%S" if with_secs else "%H:%M")


# --------------------------------------------------------------------------- #
# data layer
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


def connect(db=DB):
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


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


_PRIO = ("CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
         "WHEN 'medium' THEN 2 ELSE 3 END")


def load_snapshot(conn, root=HOME, now=None, recent=6):
    now = now or utc_now()
    projects = []
    dep = ",blocked_on" if _has_column(conn, "tasks", "blocked_on") else ""
    # Projects to render = those configured on disk (a dir under projects/ with a `roles` file —
    # the same test the router and linter use) UNIONed with any project referenced by a task. The
    # union keeps a configured-but-taskless project visible in the panel, and still surfaces an
    # "orphaned" project whose directory was removed but whose tasks linger on the board.
    pdir = os.path.join(root, "projects")
    on_disk = ([d for d in os.listdir(pdir)
                if os.path.exists(os.path.join(pdir, d, "roles"))]
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
            "SELECT id,started_at,ended_at,agent,status,summary,log_path FROM runs "
            "WHERE project=? ORDER BY id DESC LIMIT ?", (name, recent)).fetchall()
        proj_runs = [Run(id=r["id"], started_at=r["started_at"], agent=r["agent"],
                         status=r["status"], summary=r["summary"],
                         log_path=r["log_path"], project=name,
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
        "SELECT id,started_at,ended_at,project,agent,status,summary,log_path FROM runs "
        "ORDER BY id DESC LIMIT ?", (recent,)).fetchall()
    recent_runs = [Run(id=r["id"], started_at=r["started_at"],
                       agent=f"{r['project']}/{r['agent']}",
                       status=r["status"], summary=r["summary"],
                       log_path=r["log_path"], project=r["project"],
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
    rows = conn.execute(
        "SELECT started_at,ended_at,project,agent,status,summary,log_path FROM runs "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [Run(started_at=r["started_at"],
                agent=f"{r['project']}/{r['agent']}",
                status=r["status"], summary=r["summary"],
                log_path=r["log_path"], project=r["project"],
                dur_min=minutes_between(r["started_at"], r["ended_at"]))
            for r in rows]


# --------------------------------------------------------------------------- #
# plain renderer (the cleaned-up one-shot `dais status`)
# --------------------------------------------------------------------------- #


def color_enabled():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def colors(enabled):
    keys = ("C0", "CB", "CD", "CR", "CG", "CY", "CM", "CC", "CW")
    if not enabled:
        return {k: "" for k in keys}
    return dict(C0="\033[0m", CB="\033[1m", CD="\033[2m", CR="\033[31m",
                CG="\033[32m", CY="\033[33m", CM="\033[35m", CC="\033[36m",
                CW="\033[97m")


def _ids(tasks):
    return [t.id for t in tasks]


def _banner(label, width=45):
    """Center ` label ` in a rule of `═` `width` display columns wide. width=45 with
    label='DAIS · STATUS' reproduces the original banner exactly."""
    mid = f" {label} "
    pad = max(0, width - disp_width(mid))
    left = pad // 2
    return "═" * left + mid + "═" * (pad - left)


def render_plain(snap, color=None):
    if color is None:
        color = color_enabled()
    c = colors(color)
    out = []

    def P(s=""):
        out.append(s)

    P()
    label = f"DAIS · {snap.workspace}" if snap.workspace else "DAIS · STATUS"
    P(f"{c['CB']}{c['CM']}{_banner(label)}{c['C0']}")
    P()
    for p in snap.projects:
        bs = p.tasks_by_status
        P(f"{c['CB']}{c['CW']}▌ {p.name}{c['C0']}")
        if p.stage_goal:
            P(f"  {c['CD']}{truncate_words(p.stage_goal, 84)}{c['C0']}")
        P()
        if p.running:
            for agent, since in p.running:
                el = fmt_elapsed(seconds_between(since, snap.ts))
                P(f"  {c['CG']}{c['CB']}▶ RUNNING NOW{c['C0']}  {agent}  "
                  f"{c['CD']}({el} · since {to_local_hhmm(since)}){c['C0']}")
        else:
            P(f"  {c['CD']}▶ idle{c['C0']}")
        if snap.cap_state:
            P(f"  {c['CY']}⏸ cooling down — recent cap "
              f"(resumes when the window frees){c['C0']}")
        # phases in the machine's declared (flow) order; ◆ marks founder-gate phases (NEEDS YOU).
        m = p.machine or {}
        states = m.get("states", {})
        gate = {s for s, meta in states.items()
                if not meta.get("terminal") and MC.band_of(m, s) == "NEEDS YOU"}
        for st, meta in states.items():
            if meta.get("terminal"):
                continue
            ids = _ids(bs.get(st, []))
            if not ids:
                continue
            oc = (c['CW'] + c['CB']) if st in gate else c['CY']
            mark = "◆ " if st in gate else ""
            P(f"  {oc}{mark}{st.replace('_', ' ')}:{c['C0']}  {collapse_ids(ids, 12)}")
        for st in sorted(bs):                       # any populated state the machine doesn't declare
            if st in states:
                continue
            ids = _ids(bs[st])
            if ids:
                P(f"  {c['CC']}• {st.replace('_', ' ')}:{c['C0']}  {collapse_ids(ids, 12)}")
        # archive summary — the machine's TERMINAL states (not hardcoded done/cancelled, so a
        # machine whose terminal is e.g. `published` still shows its finished work)
        term_segs = [f"{st.replace('_', ' ')}: {len(bs.get(st, []))}"
                     for st, meta in states.items() if meta.get("terminal") and bs.get(st)]
        if term_segs:
            P(f"  {c['CG']}✅ {term_segs[0]}{c['C0']}"
              + "".join(f"  ·  {c['CD']}{s}{c['C0']}" for s in term_segs[1:]))
        P()
    P(f"  {c['CB']}recent runs{c['C0']} {c['CD']}"
      f"(newest first · time · agent · result · dur · what it did){c['C0']}")
    for r in snap.recent_runs:
        t = to_local_hhmm(r.started_at)
        if r.status == "succeeded":
            rc = c['CG']
        elif r.status in ("capped", "failed"):
            rc = c['CR']
        elif r.status == "interrupted":
            rc = c['CY']
        else:
            rc = c['C0']
        dur = f"{r.dur_min}m" if r.dur_min is not None else "··"
        summ = (r.summary or ("(running)" if r.status == "running" else "—")).replace("→", " → ")
        P(f"    {c['CD']}{t}{c['C0']}  {r.agent:<20} {rc}{r.status:<12}{c['C0']} "
          f"{c['CD']}{dur:<4}{c['C0']} {summ}")
    P(f"  {c['CD']}history: git -C <project repo> log --oneline | head"
      f"   ·   a run: dais logs <project>{c['C0']}")
    P(f"{c['CB']}{c['CM']}"
      f"═══════════════════════════════════════════════{c['C0']}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# TUI-support pure functions
# --------------------------------------------------------------------------- #
# task priorities low→critical (the `+`/`-` cycle + the set-priority picker)
PRIORITIES = ("low", "medium", "high", "critical")

# short, scannable tokens for the contextual action bar / confirm prompts, keyed by
# action id. Falls back to the engine's full label for anything not listed.
BAR_LABEL = {
    "approve": "approve", "reject": "reject", "accept": "accept",
    "request_changes": "request-changes", "start": "start",
    "undefer": "un-defer", "unblock": "unblock",
    "defer": "defer", "cancel": "cancel", "cancel_run": "cancel-run",
}


def bar_label(act):
    """The action's short bar token text (engine label as a fallback)."""
    return BAR_LABEL.get(act.id, act.label)

# a live-log line worth flagging red (a failed command, a raised error, a non-zero exit)
_LOG_ERR_RE = re.compile(r"exit code [1-9]|\bfatal\b|traceback|exception|\berror\b", re.I)


def gate_count(snap, running_ids=frozenset()):
    """Total founder-gate tasks across all projects (the ⚡ NEEDS YOU count) — derived from each
    project's machine (a state whose band is NEEDS YOU: no dispatch role, a founder edge). Running
    tasks are excluded so the header count matches the gate band body."""
    n = 0
    for p in snap.projects:
        for st, tasks in p.tasks_by_status.items():
            if p.machine and MC.band_of(p.machine, st) == "NEEDS YOU":
                n += sum(1 for t in tasks if (p.name, t.id) not in running_ids)
    return n


def watch_args(interval, par):
    """Validated/clamped argv tail for `dais watch <interval> <par>`: par is clamped
    to 1–5, interval to a positive int (a non-int / non-positive value falls back to
    the 300s default). Pure, so the cockpit's `w` flow can be unit-tested."""
    try:
        iv = int(interval)
    except (TypeError, ValueError):
        iv = 300
    if iv < 1:
        iv = 300
    try:
        p = int(par)
    except (TypeError, ValueError):
        p = 1
    p = max(1, min(5, p))
    return [str(iv), str(p)]


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


def filter_rows(rows, term, key):
    term = term.lower()
    return [r for r in rows if term in (key(r) or "").lower()]


def short_summary(summary, limit=1):
    """Collapse a comma-joined run summary to first item + ' (+N more)', spacing arrows."""
    if not summary:
        return ""
    parts = [p.strip() for p in summary.split(",") if p.strip()]
    if len(parts) <= limit:
        out = summary
    else:
        out = ", ".join(parts[:limit]) + f"  (+{len(parts) - limit} more)"
    return out.replace("→", " → ")


# --------------------------------------------------------------------------- #
# running-visibility + list-clarity helpers (pure)
# --------------------------------------------------------------------------- #
def running_task_id(project):
    """Fallback guess at the task a running agent is on (the run's recorded claim in run_tasks is the
    real signal; this only covers the window before it records one): the first task in a state the
    machine auto-dispatches (band QUEUED), else ''."""
    for st, tasks in project.tasks_by_status.items():
        if tasks and MC.band_of(project.machine, st) == "QUEUED":
            return tasks[0].id
    return ""


def running_threads(snap, now=None, root=HOME):
    """Every concurrent agent across all projects -> dicts for the RUNNING band. The task each agent
    is on is guessed agent-awarely (from the role's handled statuses) so it isn't mislabeled; a
    non-role runner like 'deploy' is task-less."""
    now = now or utc_now()
    out = []
    for p in snap.projects:
        live_log = (p.recent_runs[0].log_path
                    if p.recent_runs and p.recent_runs[0].status == "running" else None)
        for agent, since in p.running:
            # Authoritative first: the task THIS running agent recorded via run_tasks — its claim, else
            # the first task it touched. Fall back to the machine-derived guess only before it records.
            rr = next((x for x in p.recent_runs
                       if x.status == "running" and x.agent == agent), None)
            task = (rr.claim or (rr.task_ids[0] if rr.task_ids else "")) if rr else ""
            if not task:
                task = running_task_id(p)
            out.append(dict(project=p.name, agent=agent, since=since,
                            secs=seconds_between(since, now),
                            task=task, log_path=live_log))
    return out


def find_task(snap, project, task_id):
    """The Task object for (project, task_id), or None."""
    if not task_id:
        return None
    for p in snap.projects:
        if p.name == project:
            for lst in p.tasks_by_status.values():
                for t in lst:
                    if t.id == task_id:
                        return t
    return None


def tail_lines(path, n=15, maxbytes=65536):
    """Last n lines of a (possibly growing) log file; reads only the tail."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - maxbytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def file_sig(path):
    """(size, mtime) of a file, or None — a cheap change-token for caching."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# control helpers (T1-T3) — pure; the App methods shell out to ./dais
# --------------------------------------------------------------------------- #
WATCH_PID = os.path.join("projects", ".watch.pid")
WATCH_LOG = os.path.join("projects", ".watch.log")
PAUSED = os.path.join("projects", ".paused")


def parse_pr(pr_url):
    """Extract a PR number from a pr_url ('/pull/42' or a trailing number), else ''."""
    if not pr_url:
        return ""
    m = re.search(r"/pull/(\d+)", pr_url) or re.search(r"\b(\d+)\s*$", pr_url)
    return m.group(1) if m else ""


def project_roles(root, project):
    """Role names declared in projects/<project>/roles, in file order (run-a-role picker)."""
    roles = []
    try:
        with open(os.path.join(root, "projects", project, "roles")) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    roles.append(line.split()[0])
    except OSError:
        pass
    return roles


def watch_state(root):
    """Return (state, interval, par): 'paused' if the sentinel is set, else 'running'
    if .watch.pid holds a live pid, else 'stopped'. interval/par come from the pidfile."""
    paused = os.path.exists(os.path.join(root, PAUSED))
    interval = par = None
    alive = False
    try:
        with open(os.path.join(root, WATCH_PID)) as fh:
            parts = fh.read().split()
        pid = int(parts[0])
        interval = parts[1] if len(parts) > 1 else None
        par = parts[2] if len(parts) > 2 else None
        alive = _pid_alive(pid)
    except (OSError, ValueError, IndexError):
        alive = False
    if paused:
        return ("paused", interval, par)
    if alive:
        return ("running", interval, par)
    return ("stopped", interval, par)


# --------------------------------------------------------------------------- #
# curses TUI (`dais top`) — manual-smoke verified
# --------------------------------------------------------------------------- #
def _open_cmd():
    if sys.platform == "darwin":
        return "open"
    from shutil import which
    return "xdg-open" if which("xdg-open") else None


def _wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width) or [""]


def _char_cols(ch):
    """Display columns for one character: 0 for combining/zero-width/control,
    2 for East-Asian wide/fullwidth, else 1."""
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(s):
    """Total display columns a string occupies in a monospace terminal."""
    return sum(_char_cols(c) for c in s)


def clip_cols(s, cols):
    """Longest prefix of `s` that fits in `cols` display columns, with control
    characters neutralised to spaces. Truncating by display WIDTH (not character
    count) — and dropping escapes/tabs — is what stops a wide glyph or stray
    control sequence from overrunning a pane and wrapping into the next row,
    which is the source of the curses 'bleed' artifacts."""
    if cols <= 0:
        return ""
    out, used = [], 0
    for ch in s:
        if ord(ch) < 32 or ord(ch) == 127:
            ch = " "
        cw = _char_cols(ch)
        if used + cw > cols:
            break
        out.append(ch)
        used += cw
    return "".join(out)


def pad_cols(s, cols):
    """clip_cols, then pad with spaces to exactly `cols` display columns so the
    row (and any reverse-video highlight) clears its full width every frame."""
    clipped = clip_cols(s, cols)
    return clipped + " " * max(0, cols - disp_width(clipped))


def wrap_cols(s, cols, subsequent_indent=""):
    """Wrap `s` into lines of at most `cols` display columns. Breaks on a space
    near the right edge when one is there, hard-breaks an over-long token (paths,
    URLs, JSON) otherwise, and prefixes continuation lines with `subsequent_indent`
    so a wrapped entry still reads as one. Width-aware, so wrapping never overruns
    the pane the way a naive char-count wrap would."""
    if cols <= 0:
        return [s]
    s = s.rstrip()
    if not s:
        return [""]
    out, indent = [], ""
    while s:
        budget = max(1, cols - disp_width(indent))
        fit = clip_cols(s, budget)
        if len(fit) >= len(s):
            out.append(indent + s)
            break
        brk = s[:len(fit)].rfind(" ")
        if brk > budget // 3:                 # only break on a space that isn't too early
            line, s = s[:brk], s[brk + 1:]
        else:
            line, s = s[:len(fit)], s[len(fit):]
        out.append(indent + line)
        indent = subsequent_indent
    return out or [""]


def _add(scr, y, x, s, w, attr=0):
    """Write `s` at (y, x), clipped to the columns available before the row's
    last cell. Clipping by display width (and never writing the final column)
    keeps a wide glyph or stray escape from overrunning into the next line."""
    try:
        scr.addstr(y, x, clip_cols(s, max(0, w - x - 1)), attr)
    except curses.error:
        pass


class App:
    def __init__(self, scr, interval=2.0, root=HOME, conn=None):
        self.scr = scr
        self.interval = max(0.5, interval)
        self.root = root
        self.conn = conn or connect()
        self.snap = None
        self.sel_id = None              # selection tracked by id across refreshes
        self.detail_scroll = 0
        self.filter = ""
        self.filtering = False
        self._flash_msg = ""
        self._flash_until = 0.0
        self.last_fetch = 0.0
        self.has_color = False

    @property
    def flash(self):
        return self._flash_msg

    @flash.setter
    def flash(self, msg):
        # any feedback message stays visible ~4s, surviving the 2s auto-refresh
        self._flash_msg = msg
        self._flash_until = (time.monotonic() + 4.0) if msg else 0.0

    # ---- color ----
    def _init_colors(self):
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            self.has_color = False
            return
        self.has_color = True
        palette = {1: curses.COLOR_GREEN, 2: curses.COLOR_RED,
                   3: curses.COLOR_CYAN, 4: curses.COLOR_YELLOW,
                   5: curses.COLOR_MAGENTA, 6: curses.COLOR_WHITE}
        for pid, col in palette.items():
            curses.init_pair(pid, col, -1)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_CYAN)
        # pair 8: the panel's vitals readout bar — bold white on blue (a classic statusline). High,
        # reliable contrast in any colour depth; distinct from white (focus) and cyan (band headers).
        curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLUE)

    def _cp(self, n):
        return curses.color_pair(n) if self.has_color else 0

    def _now(self):
        # UTC to match DB stamps; recomputed each frame so running clocks tick live.
        return utc_now()

    def _log_attr(self, line):
        """Colour a live-log line by its fmt-stream marker so the stream is
        scannable: 💬 assistant narration (cyan), 🔧 tool call (yellow),
        ↳ tool output (dim grey), ✓ done (green bold); anything that smells
        like a failure (errors, non-zero exit) goes red so it jumps out."""
        s = line.lstrip()
        if s.startswith("💬"):
            return self._cp(3)
        if s.startswith("🔧"):
            return self._cp(4)
        if s.startswith("✓"):
            return self._cp(1) | curses.A_BOLD
        if s.startswith("↳"):
            return self._cp(2) if _LOG_ERR_RE.search(s) else (self._cp(6) | curses.A_DIM)
        return self._cp(2) if _LOG_ERR_RE.search(s) else 0

    # ---- data ----
    def refresh(self):
        try:
            self.snap = load_snapshot(self.conn, root=self.root)
        except sqlite3.Error as e:
            self.flash = f"db busy: {e}"      # keep last snapshot (flash auto-expires)
        self.last_fetch = time.monotonic()

    def _selectable(self, rows):
        return [i for i, r in enumerate(rows) if r.get("sel", True)]

    def _selected(self, rows):
        for i, r in enumerate(rows):
            if r["id"] == self.sel_id and r.get("sel", True):
                return i, r
        sel = self._selectable(rows)
        return (sel[0], rows[sel[0]]) if sel else (0, None)

    def _move_sel(self, rows, sel_i, delta):
        """Id of the next/prev SELECTABLE row (skips status-group headers)."""
        sels = self._selectable(rows)
        if not sels:
            return self.sel_id
        pos = sels.index(sel_i) if sel_i in sels else 0
        pos = max(0, min(pos + delta, len(sels) - 1))
        return rows[sels[pos]]["id"]

    # ---- detail ----
    def detail_lines(self, row):
        if not row or not self.snap:
            return ["(nothing selected)"]
        now = self._now()
        by_name = {p.name: p for p in self.snap.projects}
        if row["kind"] == "header":
            return [row["label"].strip(), "", "(status group — pick a task below)"]
        if row["kind"] == "project":
            p = by_name[row["project"]]
            out = [p.name, truncate_words(p.stage_goal, 60), ""]
            if p.running:
                for a, since in p.running:
                    el = fmt_elapsed(seconds_between(since, now))
                    tid = running_task_id(p)
                    suffix = f"  · {tid}  (↑/k to the ▶ running row up top for the live log)" if tid else ""
                    out.append(f"▶ {a} running {el}{suffix}")
            else:
                out.append("idle")
            out.append("")
            # task buckets in the machine's declared (flow) order, then any populated
            # state the machine doesn't declare — derived, not a fixed legacy status
            # list, so it stays correct for whatever machine this project runs.
            states = (p.machine or {}).get("states", {})
            for st in list(states) + [s for s in sorted(p.tasks_by_status)
                                      if s not in states]:
                ids = [t.id for t in p.tasks_by_status.get(st, [])]
                if ids:
                    out.append(f"{st.replace('_', ' ')}: {collapse_ids(ids, 10)}")
            out += ["", "recent runs:"]
            for r in p.recent_runs:
                dur = f"{r.dur_min}m" if r.dur_min is not None else "··"
                out.append(f"  {to_local_hhmm(r.started_at):<5} {r.agent:<10} "
                           f"{r.status:<11} {dur:<4} {short_summary(r.summary)}")
            return out
        task = row["task"]
        p = by_name[row["project"]]
        out = [f"{task.id}  {task.status}",
               f'"{task.title}"',
               f"assignee {task.assignee or '-'} · prio {task.priority} · "
               f"pr {task.pr_url or '(none)'}",
               ""]
        if task.notes:
            out.append("notes:")
            out += ["  " + ln for ln in _wrap(task.notes, 56)]
            out.append("")
        out.append(f"runs touching {task.id}:")
        for r in runs_touching(p.recent_runs, task.id):
            dur = f"{r.dur_min}m" if r.dur_min is not None else "··"
            out.append(f"  {to_local_hhmm(r.started_at):<5} {r.agent:<10} "
                       f"{r.status:<11} {dur:<4}")
        return out

    def running_header(self, row, now):
        """Fixed header lines shown above a running thread's live log (the log itself is
        rendered separately so a streaming tail never pushes this around)."""
        task = find_task(self.snap, row["project"], row.get("task_id"))
        el = fmt_elapsed(seconds_between(row["since"], now))
        tid = row.get("task_id") or "—"
        head = [f"{tid}  {row['project']}/{row['agent']} · running {el}"]
        if task:
            head.append(f'"{truncate_words(task.title, 56)}"')
        return head

    # ---- draw ----
    def launch_logs(self, row):
        if not row or not self.snap:
            return
        p = {pr.name: pr for pr in self.snap.projects}[row["project"]]
        path = p.recent_runs[0].log_path if p.recent_runs else None
        if not path or not os.path.exists(path):
            self.flash = "no log file for this project"
            return
        pager = os.environ.get("PAGER", "less")
        curses.endwin()
        subprocess.call([pager, "+G", path])
        self.scr.refresh()
        curses.doupdate()

    def launch_pr(self, row):
        url = row and row.get("task") and row["task"].pr_url
        if not url:
            self.flash = "no PR url for this item"
            return
        cmd = _open_cmd()
        if not cmd:
            self.flash = "no opener (open/xdg-open) found"
            return
        subprocess.Popen([cmd, url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    # ---- control (T1-T3): every action shells out to ./dais ----
    def _dais(self):
        return os.path.join(ROOT, "dais")   # the binary is TOOL CODE -> DAIS_ROOT, not the data dir

    def _confirm(self, msg):
        h, w = self.scr.getmaxyx()
        # consistent padding: blank row top + bottom, a 2-space left margin (matches ? help / menu)
        lines = ["", f"  {msg}  [y/N]", ""]
        bw = max(disp_width(s) for s in lines) + 2
        y0, x0 = max(0, h // 2 - 1), max(0, (w - bw) // 2)
        self.scr.timeout(-1)
        for i, ln in enumerate(lines):
            attr = curses.A_REVERSE | (curses.A_BOLD if i == 1 else 0)
            _add(self.scr, y0 + i, x0, pad_cols(ln, bw), x0 + bw, attr)
        self.scr.refresh()
        ch = self.scr.getch()
        self.scr.timeout(250)
        return ch in (ord("y"), ord("Y"))

    def _menu(self, title, options, keys=None):
        """Tiny modal picker: returns the chosen index or None. If `keys` is given (one per option,
        '' = none), each option shows its KEY and pressing that key selects it — so the action menu
        mirrors the bar's letters instead of a separate 1-9 scheme. 1-9 still works as a fallback."""
        if not options:
            return None
        h, w = self.scr.getmaxyx()
        def _label(i, o):
            k = keys[i] if keys and i < len(keys) else ""
            return f"  {k} · {o}" if k else f"  {i + 1}. {o}"
        # consistent padding: blank row top + bottom, a 2-space left margin on every line.
        lines = ["", "  " + title] + [_label(i, o) for i, o in enumerate(options[:9])]
        lines += ["  (press the key, or 1-9 · esc cancel)", ""]
        bw = max(disp_width(s) for s in lines) + 2
        y0, x0 = max(0, h // 2 - len(lines) // 2), max(0, (w - bw) // 2)
        self.scr.timeout(-1)
        for i, ln in enumerate(lines):
            attr = curses.A_REVERSE | (curses.A_BOLD if i == 1 else 0)   # the title row (after blank top)
            _add(self.scr, y0 + i, x0, pad_cols(ln, bw), w, attr)
        self.scr.refresh()
        ch = self.scr.getch()
        self.scr.timeout(250)
        if keys:                                  # a pressed action-key selects its option
            for i, k in enumerate(keys[:9]):
                if k and ch == ord(k):
                    return i
        if ord("1") <= ch <= ord("9"):
            idx = ch - ord("1")
            return idx if idx < len(options[:9]) else None
        return None

    def _stop_watch(self):
        """SIGTERM the watch process GROUP so its stop() trap fires immediately. watch
        is usually parked in `sleep`, and bash defers a trap until its foreground child
        returns; killpg kills the sleep too (like Ctrl-C), so teardown is graceful."""
        try:
            with open(os.path.join(self.root, WATCH_PID)) as fh:
                pid = int(fh.read().split()[0])
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            self.flash = "watch stopping…"
        except (OSError, ValueError):
            self.flash = "could not stop watch (stale pidfile?)"

    def start_or_stop_watch(self):
        state, cur_i, cur_p = watch_state(self.root)
        if state in ("running", "paused"):
            idx = self._menu(f"watch is {state} ({cur_i or '?'}s ×{cur_p or '?'})",
                             ["stop watch", "reconfigure (restart)"])
            if idx is None:
                return
            self._stop_watch()
            if idx == 0:                     # stop only
                return
            # idx == 1 → reconfigure: fall through to prompt + start fresh
        iv = self._prompt(f"watch interval seconds [{cur_i or '300'}]") or (cur_i or "300")
        par = self._prompt(f"parallel agents 1-5 [{cur_p or '1'}]") or (cur_p or "1")
        args = watch_args(iv, par)
        try:
            log = open(os.path.join(self.root, WATCH_LOG), "a")
            subprocess.Popen([self._dais(), "watch"] + args,
                             stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            self.flash = f"watch started ({args[0]}s ×{args[1]})"
        except OSError as e:
            self.flash = f"start failed: {e}"

    def toggle_pause(self):
        verb = "resume" if os.path.exists(os.path.join(self.root, PAUSED)) else "pause"
        # silence the CLI's stdout echo — otherwise it prints onto the curses screen at the
        # cursor position, corrupting the layout. The flash below is the in-panel feedback.
        subprocess.run([self._dais(), verb],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.flash = "resumed" if verb == "resume" else "paused"

    def tick_project(self, proj):
        if not proj:
            return
        try:
            log = open(os.path.join(self.root, WATCH_LOG), "a")
            subprocess.Popen([self._dais(), "tick", proj], stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            self.flash = f"ticked {proj}"
        except OSError as e:
            self.flash = f"tick failed: {e}"

    def run_role(self, proj):
        """Launch a SPECIFIC role on a project (vs `t`, which lets the router pick)."""
        if not proj:
            return
        if running_agents(os.path.join(self.root, "projects", proj)):
            self.flash = f"{proj} already has a running agent"
            return
        roles = [r for r in project_roles(self.root, proj) if r != "founder"]
        idx = self._menu(f"run which role in {proj}?", roles)
        if idx is None:
            return
        role = roles[idx]
        try:
            log = open(os.path.join(self.root, WATCH_LOG), "a")
            subprocess.Popen([self._dais(), "run", proj, role], stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            self.flash = f"running {proj}/{role}"
        except OSError as e:
            self.flash = f"run failed: {e}"

    def cancel_run(self, proj):
        if not proj:
            return
        if not running_agents(os.path.join(self.root, "projects", proj)):
            self.flash = f"nothing running in {proj}"
            return
        if not self._confirm(f"cancel running agent in {proj}?"):
            return
        subprocess.call([self._dais(), "cancel", proj])
        self.flash = f"cancelled {proj}"

    # ---- the action engine (contextual bar + shared dispatcher) ----
    def _row_kind(self, row):
        """The engine's kind for a left row: 'project' | 'running' | 'task' (None for
        non-actionable rows like status-group headers)."""
        k = row.get("kind") if row else None
        return k if k in ("project", "running", "task") else None

    def _task_of(self, row):
        """The task Mapping `action_command`/`task_actions` need ({id,project,status,
        pr_url,priority}), or None for project / header rows. A running row resolves
        its in-flight task object when one exists (so its priority is real)."""
        kind = self._row_kind(row)
        if kind in (None, "project"):
            return None
        task = row.get("task")
        if task is None and kind == "running":
            task = (find_task(self.snap, row.get("project"), row.get("task_id"))
                    if self.snap else None)
        if task is not None:
            return {"id": task.id, "project": row.get("project"), "status": task.status,
                    "pr_url": task.pr_url, "priority": task.priority}
        # a running row whose task can't be resolved (task_id is '—'/None)
        return {"id": row.get("task_id"), "project": row.get("project"),
                "status": row.get("status"), "pr_url": None, "priority": None}

    def _machine_of(self, row):
        """The authored machine for a row's project — the project's own, else the coding default (so
        the action bar works even without a loaded snapshot)."""
        if not row:
            return None
        for p in (self.snap.projects if self.snap else []):
            if p.name == row.get("project"):
                return p.machine
        return MC.load(MC.default_machine_path())

    def _row_actions(self, row):
        """The actions for a row (the single source the bar, menu and keys share). A machine project
        DERIVES them from the task's outgoing edges (edge_actions); legacy uses the actions.py catalog."""
        t = self._task_of(row)
        if t is None:
            return []
        kind = self._row_kind(row)
        if kind == "task":
            return _machine_actions(self._machine_of(row), t["status"])
        if kind == "running":                    # a live agent: cancel + metadata
            return [Action("cancel_run", "cancel run", "x", "reverse", True),
                    Action("set_priority", "set priority", "", "menu", False)]
        return []

    def _slot_action(self, row, slot):
        for act in self._row_actions(row):
            if act.slot == slot:
                return act
        return None

    def menu_options(self, row):
        """Labels for the Enter action menu — exactly the engine's labels, in order."""
        return [act.label for act in self._row_actions(row)]

    def action_bar(self, row):
        """The always-visible contextual footer: the keyed actions for the selected
        row as `KEY label`, then `+/- priority`, `↵ actions`, and the global `n new`.
        Project rows show the project-control keys instead."""
        kind = self._row_kind(row)
        if kind == "project":
            return "R run-role · t tick · w watch · p pause · c cancel · ↵ expand"
        acts = self._row_actions(row)
        if not acts:
            return "n new"
        parts = [f"{a.key} {bar_label(a)}" for a in acts if a.key]
        if any(a.id == "set_priority" for a in acts):
            parts.append("+/- priority")
        if any(a.slot == "menu" for a in acts):
            parts.append("↵ actions")
        parts.append("n new")
        return " · ".join(parts)

    def _dispatch(self, cmd):
        """Run a fast `dais …` command, blocking; return its exit code. The single
        subprocess seam the cockpit tests mock."""
        return subprocess.call([self._dais()] + [str(c) for c in cmd])

    def _spawn_agent(self, cmd):
        """Launch a STREAMING-agent command (e.g. `start`) DETACHED, with its output to the run log —
        never inline. A foreground `claude -p` stream inherits this terminal and corrupts the curses
        screen; backgrounding it (as the watch loop does) keeps the TUI intact, and the agent then
        shows up in RUNNING with its per-role log tailed by `L`."""
        try:
            log = open(os.path.join(self.root, WATCH_LOG), "a")
            subprocess.Popen([self._dais()] + [str(c) for c in cmd], stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError as e:
            self.flash = f"start failed: {e}"
            return False

    def do_action(self, action_id, row):
        """The shared dispatcher behind shortcuts and the Enter menu: build the argv
        from the engine, honor `Action.confirm`, run it (or route an interactive id),
        then refresh + flash."""
        if action_id == "new":               # works on any row (uses its project)
            self._new_task(row)
            return
        t = self._task_of(row)
        if t is None:
            self.flash = "no task selected"
            return
        if self._row_kind(row) == "task":
            return self._act_machine(self._machine_of(row), action_id, row, t)
        # running / project rows: cancel a live agent, or a metadata op
        if action_id == "cancel_run":
            if not self._confirm(f"cancel {t['project']}'s running agent?"):
                return
            rc = self._dispatch(["cancel", t["project"]])
            self.refresh()
            self.flash = f"cancelled {t['project']}" if rc == 0 else f"cancel failed (exit {rc})"
        elif action_id == "set_priority":
            self._priority_menu(row)
        elif action_id == "edit_title":
            self._edit_title(row)
        else:
            self.flash = f"no handler for {action_id}"

    def _act_machine(self, m, verb, row, t):
        """Execute a machine action: launch the dispatch agent (__start), a metadata op, or fire the
        edge via `dais fire`. Strong-human guards (typed_confirm/attest) prompt IN the panel for the
        same explicit input the CLI flags require; an unverifiable verify guard surfaces the shell
        command instead — the panel never self-asserts that a check ran."""
        if verb == "set_priority":
            return self._priority_menu(row)
        if verb == "edit_title":
            return self._edit_title(row)
        if verb == "__start":
            # the launch is detached (a foreground claude stream would corrupt curses), so this
            # flash claims INTENT, not success — the RUNNING band / watch log is the truth.
            if self._spawn_agent(["start", t["id"]]):
                self.flash = f"launching {t['id']} — watch RUNNING / press L for the log"
            self.refresh()
            return
        edge = next((e for e in MC.edges_from(m, t["status"]) if e.get("verb") == verb), None)
        if not edge:
            self.flash = f"no '{verb}' edge from {t['status']}"
            return
        guards = edge.get("guards", [])
        # a verify:<check> with no declared checker (machine `checks`) means "a check must have
        # RUN" — the panel can't assert that, so it surfaces the exact command instead of
        # stamping it. (Checkers that ARE declared run inside the engine on fire.)
        checks = (m or {}).get("checks", {})
        unchecked = [g for g in guards
                     if g.startswith("verify:") and g.split(":", 1)[1] not in checks]
        if unchecked:
            flags = " ".join("--verify " + g.split(":", 1)[1] for g in unchecked)
            self.flash = f"{verb} needs a verified check — run:  dais fire {t['id']} {verb} {flags}"
            return
        # strong-human guards prompt IN the panel — same strength as the CLI flags (you type
        # the task id / the fact name), without dropping to a shell to greenlight a release.
        cmd = ["fire", t["id"], verb, "--by", "founder"]
        prompted = False
        for g in guards:
            if g == "typed_confirm":
                typed = self._prompt(f"{verb} {t['id']} — type the task id to confirm")
                if typed != t["id"]:
                    self.flash = f"{verb} cancelled (typed confirmation mismatch)"
                    return
                cmd += ["--typed", t["id"]]; prompted = True
            elif g.startswith("attest:"):
                fact = g.split(":", 1)[1].split(" ")[0]
                typed = self._prompt(f"attest '{fact}' — type the fact name to assert it")
                if typed != fact:
                    self.flash = f"{verb} cancelled (attestation not given)"
                    return
                cmd += ["--attest", fact]; prompted = True
        if "confirm" in guards:
            # the typed prompt IS the confirmation when one just happened — don't double-ask
            if not prompted and not self._confirm(f"{verb} {t['id']}?"):
                return
            cmd.append("--confirm")
        rc = self._dispatch(cmd)
        self.refresh()
        self.flash = f"{verb} {t['id']}" if rc == 0 else f"{verb} failed (exit {rc})"

    def _action_menu(self, row):
        """Open the Enter menu and dispatch the chosen action. Shows each action's KEY (mirroring the
        bar) so it's the same letters, not a separate scheme; pressing the key or 1-9 both select."""
        acts = self._row_actions(row)
        if not acts:
            self.flash = "no actions for this row"
            return
        idx = self._menu(f"actions · {(self._task_of(row) or {}).get('id') or ''}".rstrip(),
                         [a.label for a in acts], keys=[a.key for a in acts])
        if idx is not None:
            self.do_action(acts[idx].id, row)

    def _bump_priority(self, row, direction):
        if self._row_kind(row) != "task":
            return
        t = self._task_of(row)
        if not t or not t.get("id"):
            return
        # respect the single source (_row_actions): if a row can't take set_priority
        # (e.g. a done/cancelled task), the +/- keys are a no-op too.
        if not any(a.id == "set_priority" for a in self._row_actions(row)):
            self.flash = "priority doesn't apply to a done/cancelled task"
            return
        newp = priority_cycle(t.get("priority"), direction)
        rc = self._dispatch(["task", "set", t["id"], "--priority", newp])
        self.refresh()
        self.flash = (f"{t['id']} → {newp}" if rc == 0 else f"priority failed (exit {rc})")

    def _priority_menu(self, row):
        t = self._task_of(row)
        if not t or not t.get("id"):
            return
        idx = self._menu(f"priority for {t['id']} (now {t.get('priority') or '?'})",
                         list(PRIORITIES))
        if idx is None:
            return
        newp = PRIORITIES[idx]
        rc = self._dispatch(["task", "set", t["id"], "--priority", newp])
        self.refresh()
        self.flash = (f"{t['id']} → {newp}" if rc == 0 else f"priority failed (exit {rc})")

    def _edit_title(self, row):
        t = self._task_of(row)
        if not t or not t.get("id"):
            return
        title = self._prompt(f"new title for {t['id']}")
        if not title:
            return
        rc = self._dispatch(["task", "set", t["id"], "--title", title])
        self.refresh()
        self.flash = (f"retitled {t['id']}" if rc == 0 else f"retitle failed (exit {rc})")

    def _new_task(self, row):
        proj = row.get("project") if row else None
        if not proj:
            self.flash = "no project for a new task"
            return
        title = self._prompt(f"new task title in {proj}")
        if not title:
            return
        rc = self._dispatch(["task", "add", proj, title])
        self.refresh()
        self.flash = (f"added task in {proj}" if rc == 0 else f"add failed (exit {rc})")

    def _prompt(self, label):
        """Read a line in the footer (esc cancels → ''). WRAPS onto multiple rows that grow upward,
        so a long title stays fully visible instead of scrolling off the right edge. The trailing
        '_' is the cursor (and keeps wrap_cols from rstrip-ing a real trailing space)."""
        buf = ""
        self.scr.timeout(-1)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        used = 1
        while True:
            h, w = self.scr.getmaxyx()
            bw = max(1, w - 1)
            lines = wrap_cols(f"  {label}: {buf}_", bw)
            lines = lines[-max(1, h - 1):]               # never taller than the screen; keep the tail
            used = max(used, len(lines))
            for i in range(used):                        # clear the region the prompt has ever used
                _add(self.scr, h - used + i, 0, pad_cols("", bw), w, 0)
            y0 = h - len(lines)                          # bottom-anchored, growing upward
            for i, ln in enumerate(lines):
                _add(self.scr, y0 + i, 0, pad_cols(ln, bw), w,
                     curses.A_REVERSE | curses.A_BOLD)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (10, 13):               # enter — accept
                break
            if ch == 27:                     # esc — cancel
                buf = ""
                break
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif 32 <= ch < 127:
                buf += chr(ch)
        self.scr.timeout(250)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        return buf.strip()

    def handle(self, ch, rows, sel_i, sel_row):
        if self.filtering:
            if ch in (10, 13):           # enter
                self.filtering = False
            elif ch == 27:               # esc
                self.filtering = False
                self.filter = ""
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.filter = self.filter[:-1]
            elif 32 <= ch < 127:
                self.filter += chr(ch)
            return True
        if ch in (ord("q"),):
            return False
        if ch in (ord("j"), curses.KEY_DOWN):
            if rows:
                self.sel_id = self._move_sel(rows, sel_i, +1)
                self.detail_scroll = 0
        elif ch in (ord("k"), curses.KEY_UP):
            if rows:
                self.sel_id = self._move_sel(rows, sel_i, -1)
                self.detail_scroll = 0
        elif ch in (10, 13):             # enter — the action menu for the selection
            if sel_row:
                self._action_menu(sel_row)
        elif ch == ord("l"):
            self.launch_logs(sel_row)
        elif ch == ord("o"):             # open the selection's PR in a browser
            self.launch_pr(sel_row)
        elif ch == ord("w"):
            self.start_or_stop_watch()
        elif ch == ord("p"):
            self.toggle_pause()
        elif ch == ord("t"):
            self.tick_project(sel_row["project"] if sel_row else None)
        elif ch == ord("R"):
            self.run_role(sel_row["project"] if sel_row else None)
        elif ch == ord("c"):
            self.cancel_run(sel_row["project"] if sel_row else None)
        elif 32 <= ch < 127 and (act := next(
                (a for a in self._row_actions(sel_row) if a.key == chr(ch)), None)):
            self.do_action(act.id, sel_row)  # any contextual action by its key (a/x/e …)
        elif ch in (ord("+"), ord("=")):     # raise priority ('=' is the unshifted '+')
            self._bump_priority(sel_row, +1)
        elif ch == ord("-"):                 # lower priority
            self._bump_priority(sel_row, -1)
        elif ch == ord("n"):                 # new task in the row's project
            self.do_action("new", sel_row)
        elif ch == ord("/"):
            self.filtering = True
            self.filter = ""
        return True

    def run(self):
        curses.curs_set(0)
        self._init_colors()
        self.scr.timeout(250)
        self.refresh()
        while True:
            if time.monotonic() - self.last_fetch >= self.interval:
                self.refresh()
            self.draw()
            ch = self.scr.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            rows = self.left_rows()
            sel_i, sel_row = self._selected(rows)
            if not self.handle(ch, rows, sel_i, sel_row):
                break


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="dashboard")
    ap.add_argument("--tui", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)
    if args.tui:
        if not sys.stdout.isatty():
            print("dais top needs an interactive terminal; use `dais status`.",
                  file=sys.stderr)
            return 2
        import panel
        curses.wrapper(lambda scr: panel.PanelApp(scr, args.interval).run())
        return 0
    with connect() as conn:
        print(render_plain(load_snapshot(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
