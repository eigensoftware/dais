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
from actions import task_actions, action_command, priority_cycle, Action  # noqa: F401
import router  # parse_roles — so the running-task guess can be agent-aware (which statuses a role owns)

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
    deploy_configured: bool = False   # project.yaml has a deploy: command
    deploy_needs: bool = None         # prod != main → needs deploy; False = up to date; None = unchecked
    deploy_checked_at: str = None     # when prod's live SHA was last fetched (.deploy-rev cache)
    deploy_migration: bool = False    # an un-deployed commit touches a migration → flag it loudly
    deploy_commits: list = field(default_factory=list)   # (sha7, subject) of what's awaiting deploy
    deploy_failed: bool = False       # the most recent deploy ATTEMPT failed (not yet superseded)
    deploy_failed_at: str = None      # when it failed


@dataclass
class Snapshot:
    projects: list
    recent_runs: list
    cap_state: bool
    ts: str
    workspace: str = None          # workspace identity (dais.yaml `workspace:`), or None


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
        dcfg, dneeds, dchecked, dmig, dcommits = deploy_state(root, name)
        dfail, dfail_at = False, None
        if dcfg:                                          # last deploy ATTEMPT failed (any status)?
            frow = conn.execute("SELECT status, ended_at FROM runs WHERE project=? AND agent='deploy' "
                                "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            if frow and frow["status"] == "failed":
                dfail, dfail_at = True, frow["ended_at"]
        projects.append(Project(name=name, stage_goal=stage_goal(root, name),
                                running=running, tasks_by_status=by_status,
                                recent_runs=proj_runs, deploy_configured=dcfg,
                                deploy_needs=dneeds, deploy_checked_at=dchecked,
                                deploy_migration=dmig, deploy_commits=dcommits,
                                deploy_failed=dfail, deploy_failed_at=dfail_at))
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
    return Snapshot(projects=projects, recent_runs=recent_runs,
                    cap_state=capped > 0, ts=now, workspace=workspace_name(root))


def read_deploy_rev(root, project):
    """(prod_sha, checked_at) from the project's .deploy-rev cache — what `dais deploy <p> --check`
    last learned the SERVER is running (and a real deploy updates to the just-shipped SHA). The panel
    reads this (never SSHes itself); ('', None) when never checked."""
    path = os.path.join(root, "projects", project, ".deploy-rev")
    try:
        with open(path) as f:
            lines = [ln.strip() for ln in f.read().splitlines()]
        return (lines[0] if lines and lines[0] else "",
                lines[1] if len(lines) > 1 and lines[1] else None)
    except OSError:
        return ("", None)


def _deploy_has_migration(root, project, repo, sha):
    """True if any un-deployed commit (sha..origin/main) touches a migration file, per the project's
    migrations_glob (default */migrations/*.sql) — so a deploy that includes a migration can be
    flagged loudly. Local git only."""
    if not (repo and sha and sha != "?"):
        return False
    glob = project_field(root, project, "migrations_glob") or "*/migrations/*.sql"
    rx = re.compile(re.escape(glob).replace(r"\*", ".*") + "$")   # glob → regex (mirrors lib.sh migrations_re)
    try:
        out = subprocess.run(
            ["git", "-C", os.path.expanduser(repo), "diff", "--name-only", "%s..origin/main" % sha],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return any(rx.search(p) for p in out.stdout.splitlines())
    except Exception:
        pass
    return False


def _deploy_commits(repo, sha, limit=25):
    """The un-deployed commits (sha..origin/main), newest first, as (sha7, subject) — exactly what a
    `dais deploy` would ship, so the panel can show what's pending instead of just a count. Local git."""
    if not (repo and sha and sha != "?"):
        return []
    try:
        out = subprocess.run(
            ["git", "-C", os.path.expanduser(repo), "log", "--pretty=format:%h\x1f%s",
             "-n", str(limit), "%s..origin/main" % sha],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return [tuple(ln.split("\x1f", 1)) for ln in out.stdout.splitlines() if "\x1f" in ln]
    except Exception:
        pass
    return []


def deploy_state(root, project):
    """(configured, needs, checked_at, has_migration, commits) — the deploy signal as YES/NO, derived
    from what the SERVER is running (the .deploy-rev cache), NOT a guessed baseline:
      configured  – the project declares a deploy: command
      needs       – True if prod's SHA differs from origin/main (commits to ship), False if up to
                    date, None if never checked (unknown — a --check resolves it)
      checked_at  – when prod's SHA was last fetched
      has_migration / commits – the migration flag + commit list (prod..origin/main) of what'd ship
    Local git only; the SSH to learn prod's SHA happens in `dais deploy --check`, not here."""
    if not project_field(root, project, "deploy"):
        return (False, None, None, False, [])
    prod_sha, checked_at = read_deploy_rev(root, project)
    if not prod_sha:
        return (True, None, checked_at, False, [])        # configured but never checked → unknown
    repo = project_field(root, project, "repo")
    commits = _deploy_commits(repo, prod_sha)
    needs = len(commits) > 0
    has_mig = _deploy_has_migration(root, project, repo, prod_sha) if needs else False
    return (True, needs, checked_at, has_mig, commits)


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
_CANON_NAMED = {"needs_qa", "ready", "changes_requested", "ready_to_merge",
                "needs_review", "proposed", "blocked", "done", "deferred", "backlog",
                "cancelled", "doing"}


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
        rtm = _ids(bs.get("ready_to_merge", []))
        if rtm:
            P(f"  {c['CG']}{c['CB']}⏳ MERGE{c['C0']} "
              f"{c['CG']}— QA-approved, your call:{c['C0']} "
              f"{c['CB']}{collapse_ids(rtm, 12)}{c['C0']}")
        nr = _ids(bs.get("needs_review", []))
        if nr:
            P(f"  {c['CW']}{c['CB']}📋 REVIEW{c['C0']} "
              f"{c['CW']}— deliverable ready (no PR), your call:{c['C0']} "
              f"{c['CB']}{collapse_ids(nr, 12)}{c['C0']}")
        prop = _ids(bs.get("proposed", []))
        if prop:
            P(f"  {c['CM']}{c['CB']}🧭 PROPOSED{c['C0']} "
              f"{c['CM']}— Lead initiative awaiting your approve before build "
              f"(dais approve <id>):{c['C0']} "
              f"{c['CB']}{collapse_ids(prop, 12)}{c['C0']}")
        blk = _ids(bs.get("blocked", []))
        if blk:
            P(f"  {c['CR']}⛔ BLOCKED on you:{c['C0']}  {collapse_ids(blk, 12)}")
        nq = _ids(bs.get("needs_qa", []))
        if nq:
            P(f"  {c['CC']}🔍 awaiting QA:{c['C0']}  {collapse_ids(nq, 12)}")
        eng = _ids(bs.get("ready", []) + bs.get("changes_requested", []))
        if eng:
            P(f"  {c['CY']}🔧 queued for Engineer:{c['C0']}  {collapse_ids(eng, 12)}")
        for st in sorted(bs):
            if st in _CANON_NAMED:
                continue
            ids = _ids(bs[st])
            if not ids:
                continue
            if st == "needs_legal":
                lbl, oc = "⚖️  awaiting legal review", c['CM']
            else:
                lbl, oc = f"• awaiting {st}", c['CC']
            P(f"  {oc}{lbl}:{c['C0']}  {collapse_ids(ids, 12)}")
        dn = _ids(bs.get("done", []))
        if dn:
            P(f"  {c['CG']}✅ DONE ({len(dn)}):{c['C0']}  "
              f"{c['CD']}{collapse_ids(dn, 8)}{c['C0']}")
        df = _ids(bs.get("deferred", []))
        if df:
            P(f"  {c['CM']}🔒 deferred (founder-parked):{c['C0']}  "
              f"{c['CD']}{collapse_ids(df, 8)}{c['C0']}")
        P(f"  {c['CD']}📦 backlog: {len(bs.get('backlog', []))}  ·  "
          f"cancelled: {len(bs.get('cancelled', []))}{c['C0']}")
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
QUEUE_ORDER = ["ready_to_merge", "needs_review", "proposed", "blocked", "needs_qa",
               "changes_requested", "ready"]
# founder-parked statuses, revealed in the cockpit only when `b` is toggled on
PARKED_ORDER = ["backlog", "deferred"]

# task priorities low→critical (the `+`/`-` cycle + the set-priority picker)
PRIORITIES = ("low", "medium", "high", "critical")

# short, scannable tokens for the contextual action bar / confirm prompts, keyed by
# action id. Falls back to the engine's full label for anything not listed.
BAR_LABEL = {
    "approve": "approve", "reject": "reject", "accept": "accept",
    "request_changes": "request-changes", "ship": "ship", "start": "start",
    "promote": "promote", "undefer": "un-defer", "unblock": "unblock",
    "defer": "defer", "cancel": "cancel", "cancel_run": "cancel-run",
    "open_pr": "PR",
}


def bar_label(act):
    """The action's short bar token text (engine label as a fallback)."""
    return BAR_LABEL.get(act.id, act.label)

# status -> curses color-pair id (pairs defined in App._init_colors). Mirrors the
# plain renderer's palette: merge=green, blocked=red, qa=cyan, engineer=yellow,
# deferred=magenta, review=white (a founder-gate, like merge, but for non-PR work).
STATUS_PAIR = {"ready_to_merge": 1, "needs_review": 6, "proposed": 5, "blocked": 2,
               "needs_qa": 3, "changes_requested": 4, "ready": 4, "deferred": 5}

# a live-log line worth flagging red (a failed command, a raised error, a non-zero exit)
_LOG_ERR_RE = re.compile(r"exit code [1-9]|\bfatal\b|traceback|exception|\berror\b", re.I)


def action_queue(snap, order=QUEUE_ORDER):
    rows = []
    for st in order:
        for p in snap.projects:
            for t in p.tasks_by_status.get(st, []):
                rows.append((p.name, t, st))
    return rows


# --- the cockpit: founder gates vs the loop's own work -------------------- #
# The four founder-gate statuses, in ⚡ NEEDS YOU display order, with icons.
GATE_ORDER = ["ready_to_merge", "needs_review", "proposed", "blocked"]
GATE_ICON = {"ready_to_merge": "⏳", "needs_review": "📋",
             "proposed": "🧭", "blocked": "⛔"}
# The loop's own in-progress statuses — collapsed to one summary line, never listed.
LOOP_SUMMARY_ORDER = ["ready", "needs_qa", "changes_requested"]


def gate_count(snap, running_ids=frozenset()):
    """Total founder-gate tasks across all projects (the ⚡ NEEDS YOU count).
    Running tasks (running_ids: a set of (project, task_id)) are excluded because
    needs_review appears in both GATE_ORDER and _INFLIGHT_ORDER — a running agent
    can sweep a needs_review task into the running band, so we exclude it here to
    keep the header ⚡N token consistent with the gate band body."""
    return sum(1 for p in snap.projects
               for st in GATE_ORDER
               for t in p.tasks_by_status.get(st, [])
               if (p.name, t.id) not in running_ids)


def loop_summary(snap, running_ids=frozenset()):
    """One-line summary of the loop's own queued work — the statuses the founder does
    NOT action — e.g. '⚙ the loop: 3 ready · 1 needs_qa — g for full board', or None
    when there is none. Zero-count statuses are omitted. Tasks currently being run
    (`running_ids`: a set of (project, task_id)) are excluded; they show in the
    running band instead, so the summary doesn't double-count them."""
    segs = []
    for st in LOOP_SUMMARY_ORDER:
        n = sum(1 for p in snap.projects for t in p.tasks_by_status.get(st, [])
                if (p.name, t.id) not in running_ids)
        if n:
            segs.append(f"{n} {st}")
    if not segs:
        return None
    return "⚙ the loop: " + " · ".join(segs) + " — g for full board"


def allclear_line(running_count):
    """The calm replacement for the ⚡ NEEDS YOU band when no gate items remain."""
    if running_count > 0:
        return f"✓ nothing needs you — the loop is running ({running_count} in flight)"
    return "✓ nothing needs you — loop idle"


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
# In-flight task heuristic: a builder parks its task in 'doing' while working; a
# reviewer works its queue in place. First non-empty wins (one agent per project).
_INFLIGHT_ORDER = ["doing", "needs_qa", "changes_requested", "needs_review"]


def _agent_handles(root, project, agent):
    """The statuses a role handles, from the project's roles file. Returns [] for a role with no
    handles, or None when the agent isn't a role at all (e.g. 'deploy') so the caller treats it as
    task-less. Guides the running-task guess so an engineer run isn't labeled as QA's/the lead's task."""
    try:
        roles, _ = router.parse_roles(os.path.join(root, "projects", project, "roles"))
    except Exception:
        return None
    for r in roles:
        if r["name"] == agent:
            return r["handles"]
    return None


def running_task_id(project, statuses=None):
    """Best guess at the task a running agent is working, or '' (the live log is the real signal).
    With `statuses` (the running role's handled statuses), guess only among 'doing' + those — so an
    engineer run shows its doing/ready task, never QA's needs_qa or the lead's needs_scoping. With
    `statuses=None`, fall back to the generic in-flight → ready → needs_scoping order."""
    if statuses is None:
        order = _INFLIGHT_ORDER + ["ready", "needs_scoping"]
    else:
        order = ["doing"] + [s for s in statuses if s != "doing"]
    for st in order:
        tasks = project.tasks_by_status.get(st)
        if tasks:
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
        roles_exist = os.path.exists(os.path.join(root, "projects", p.name, "roles"))
        for agent, since in p.running:
            # Authoritative first: the task THIS running agent recorded via run_tasks — its claim, else
            # the first task it has touched so far. Only when the run has recorded nothing yet (before
            # it claims) or predates the feature do we fall through to the heuristic guess below.
            rr = next((x for x in p.recent_runs
                       if x.status == "running" and x.agent == agent), None)
            task = (rr.claim or (rr.task_ids[0] if rr.task_ids else "")) if rr else ""
            if not task:
                handles = _agent_handles(root, p.name, agent)
                if handles is not None:              # a known role → agent-aware guess
                    task = running_task_id(p, handles)
                elif roles_exist:                    # roles file present, agent absent → non-role (deploy) → task-less
                    task = ""
                else:                                # no roles file at all → generic fallback guess
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


def last_log_line(path, width=58):
    """Last non-empty log line (trimmed) — the 'what is it doing right now' signal."""
    for ln in reversed(tail_lines(path, 40)):
        s = ln.strip()
        if s:
            return s[:width]
    return ""


def file_sig(path):
    """(size, mtime) of a file, or None — a cheap change-token for caching."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


def log_lines(path, max_lines=20000, maxbytes=8_000_000):
    """ALL lines of a log so it can be scrolled back to the first line. Reads the
    whole file at normal sizes; only a pathologically huge log is clipped to its
    last maxbytes (and last max_lines), in which case line 1 here isn't the run's
    real first line — the scroll indicator flags that with a leading '…'."""
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            truncated = size > maxbytes
            if truncated:
                fh.seek(size - maxbytes)
                fh.readline()             # drop the partial first line
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    if truncated and lines:
        lines = ["… (earlier output trimmed) …"] + lines
    return lines


def log_window(total, height, top, follow):
    """Resolve the visible slice of a scrollable log.

    Returns (start, top, follow, max_top). `follow` keeps the view pinned to the
    live tail; scrolling down past the end re-pins it. `top` is the first visible
    display line when not following; `max_top` is the furthest-back top (so Home
    jumps to 0 and the indicator can show position)."""
    height = max(1, height)
    max_top = max(0, total - height)
    if follow or top >= max_top:
        return (max_top, max_top, True, max_top)
    return (max(0, top), max(0, min(top, max_top)), False, max_top)


# left-pane per-project shape badges: M=merge-ready R=review P=proposed B=blocked Q=qa E=eng-queue
_BADGES = [("M", ("ready_to_merge",)), ("R", ("needs_review",)), ("P", ("proposed",)),
           ("B", ("blocked",)), ("Q", ("needs_qa",)), ("E", ("ready", "changes_requested"))]


def project_badges(project):
    bs = project.tasks_by_status
    parts = []
    for letter, statuses in _BADGES:
        n = sum(len(bs.get(s, [])) for s in statuses)
        if n:
            parts.append(f"{letter}{n}")
    return " ".join(parts)


def scroll_indicator(base, shown, total):
    """'12-20/63' when the list overflows the viewport, else ''."""
    if total <= shown or shown <= 0:
        return ""
    return f"{base + 1}-{min(base + shown, total)}/{total}"


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
        self.mode = "queue"            # "queue" = the cockpit (default) | "project" = the board
        self.expanded = set()           # project names expanded in project mode
        self.show_parked = False        # reveal backlog+deferred rows (toggled by `b`)
        self.focus = "left"            # "left" | "right"
        self.sel_id = None              # selection tracked by id across refreshes
        self.detail_scroll = 0
        # live-log scrolling: follow the tail by default; scroll up to read history
        self.log_follow = True
        self.log_top = 0                # first visible display line when not following
        self._log_h = 1                 # last log viewport height (for PgUp/Dn in handle)
        self._log_total = 0             # last total display lines (for End/indicator)
        self._log_cache_key = None      # (path, file_sig, dw) → cached wrapped+coloured lines
        self._log_cache = []
        self.filter = ""
        self.filtering = False
        self._flash_msg = ""
        self._flash_until = 0.0
        self.last_fetch = 0.0
        self.has_color = False

    def _reset_log_scroll(self):
        self.log_follow = True
        self.log_top = 0

    def _log_disp(self, path, dw):
        """Wrapped + colour-tagged display lines for a log, cached on (path, size,
        mtime, width) so we only re-read/re-wrap when the file grows or the pane
        resizes — not on every 250ms repaint."""
        key = (path, file_sig(path), dw)
        if key != self._log_cache_key:
            disp = []
            for ln in log_lines(path):
                attr = self._log_attr(ln)
                lead = len(ln) - len(ln.lstrip(" "))
                cont = " " * min(lead + 3, 9)
                for piece in wrap_cols(ln, dw, subsequent_indent=cont):
                    disp.append((piece, attr))
            self._log_cache, self._log_cache_key = disp, key
        return self._log_cache

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

    def _row_attr(self, r):
        if r["kind"] == "header":
            lab = r.get("label", "").lstrip()
            if lab.startswith("⚡"):                 # the NEEDS YOU banner — make it pop
                return curses.A_BOLD | self._cp(7)
            if lab.startswith(("⚙", "✓")):           # loop summary / all-clear — calm
                return curses.A_DIM | self._cp(6)
            return curses.A_DIM | self._cp(STATUS_PAIR.get(r["status"], 6))
        if r["kind"] == "project":
            return curses.A_BOLD | (self._cp(1) if r.get("running") else self._cp(6))
        return self._cp(STATUS_PAIR.get(r["status"], 6))

    def _line_attr(self, ln):
        s = ln.strip()
        if s.startswith("▶"):
            return self._cp(1)
        head = s.split(":", 1)[0]
        if head in STATUS_PAIR:
            return self._cp(STATUS_PAIR[head])
        words = s.split()
        if len(words) >= 2 and words[1] in STATUS_PAIR:
            return self._cp(STATUS_PAIR[words[1]])
        return 0

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

    def _header_row(self, label, status):
        return dict(id=f"__hdr::{label}", kind="header", project=None, task=None,
                    status=status, running=False, sel=False, label=label)

    def left_rows(self):
        """Rows: dicts {id, kind, label, project, task, status, running, sel}.
        kind in {running, project, task, header}; headers aren't selectable (sel=False).
        Live threads lead as a '▶ running' group (select one to watch its log); the rest
        is grouped by status with counts and project shape-badges (M/R/B/Q/E)."""
        rows = []
        if not self.snap:
            return rows
        now = self._now()
        order = QUEUE_ORDER + PARKED_ORDER if self.show_parked else QUEUE_ORDER
        threads = running_threads(self.snap, now, self.root)
        running_ids = {(t["project"], t["task"]) for t in threads if t["task"]}
        if threads:
            rows.append(self._header_row(f"▶ running ({len(threads)})", "ready_to_merge"))
            for t in threads:
                tid = t["task"] or "—"
                rows.append(dict(id=f"run::{t['project']}", kind="running",
                                 project=t["project"], agent=t["agent"], since=t["since"],
                                 log_path=t["log_path"], task_id=t["task"], task=None,
                                 status="doing", running=True, sel=True,
                                 label=f"  {tid:<7} {t['project'][:9]:<9} "
                                       f"{t['agent']} {fmt_elapsed(t['secs'])}"))

        def keep(proj, task):                 # don't repeat a thread's task in its status group
            return (proj, task.id) not in running_ids

        if self.mode == "queue":
            byst = {}
            for (proj, task, st) in action_queue(self.snap, order):
                if keep(proj, task):
                    byst.setdefault(st, []).append((proj, task))

            def emit_group(label, st, items):
                rows.append(self._header_row(label, st))
                for (proj, task) in items:
                    title = truncate_words(task.title, 40)
                    rows.append(dict(id=task.id, kind="task", project=proj, task=task,
                                     status=st, running=False, sel=True,
                                     label=f"  {task.id:<7} {proj[:9]:<9} {title}"))

            # Band B — ⚡ NEEDS YOU (the four founder gates), or the all-clear line.
            gate_items = [(st, byst.get(st, [])) for st in GATE_ORDER]
            if any(items for _st, items in gate_items):
                rows.append(self._header_row(
                    f"⚡ NEEDS YOU ({gate_count(self.snap, running_ids)})", "ready_to_merge"))
                for st, items in gate_items:
                    if items:
                        emit_group(f"{GATE_ICON[st]} {st} ({len(items)})", st, items)
            else:
                rows.append(self._header_row(allclear_line(len(threads)), "ready"))

            # Band C — ⚙ the loop (collapsed; its tasks are summarised, never listed).
            summary = loop_summary(self.snap, running_ids)
            if summary:
                rows.append(self._header_row(summary, "ready"))

            # Founder-parked work (backlog/deferred) — revealed only by `b`, as rows.
            if self.show_parked:
                for st in PARKED_ORDER:
                    items = byst.get(st, [])
                    if items:
                        emit_group(f"{st} ({len(items)})", st, items)
        else:
            for p in self.snap.projects:
                run = ""
                if p.running:
                    a, since = p.running[0]
                    run = f"  ▶ {fmt_elapsed(seconds_between(since, now))}"
                badges = project_badges(p)
                bl = f"   {badges}" if badges else ""
                rows.append(dict(id=p.name, kind="project", project=p.name, task=None,
                                 status=None, running=bool(p.running), sel=True,
                                 label=f"{p.name}{run}{bl}"))
                if p.name in self.expanded:
                    byst = {}
                    for (proj, task, st) in action_queue(self.snap, order):
                        if proj == p.name and keep(proj, task):
                            byst.setdefault(st, []).append(task)
                    for st in order:
                        items = byst.get(st)
                        if not items:
                            continue
                        rows.append(self._header_row(f"  {st} ({len(items)})", st))
                        for task in items:
                            title = truncate_words(task.title, 44)
                            rows.append(dict(id=task.id, kind="task", project=p.name,
                                             task=task, status=st, running=False, sel=True,
                                             label=f"    {task.id:<7} {title}"))
        if self.filter:                       # filtering flattens to matching task rows
            rows = [r for r in rows if r["kind"] in ("task", "running")]
            rows = filter_rows(rows, self.filter, key=lambda r: r["label"])
        return rows

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
            for st in QUEUE_ORDER + ["done", "deferred", "backlog", "cancelled"]:
                ids = [t.id for t in p.tasks_by_status.get(st, [])]
                if ids:
                    out.append(f"{st}: {collapse_ids(ids, 10)}")
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
    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        now = self._now()
        cap = " · ⏸ COOLING" if (self.snap and self.snap.cap_state) else ""
        clk = to_local_hhmm(self.snap.ts, with_secs=True) if self.snap else ""
        wstate, wint, wpar = watch_state(self.root)
        if wstate == "running":
            badge = f"● watch {wint or '?'}s ×{wpar or '?'}"
        elif wstate == "paused":
            badge = "⏸ PAUSED"
        else:
            badge = "○ watch stopped"
        threads = running_threads(self.snap, now, self.root) if self.snap else []
        run = f" · ▶ {len(threads)} running" if threads else ""
        running_ids = {(t["project"], t["task"]) for t in threads if t["task"]}
        ng = gate_count(self.snap, running_ids) if self.snap else 0
        gates = f" · ⚡{ng}" if ng else ""
        ws = self.snap.workspace if self.snap else None
        ident = f"DAIS · {ws} · LIVE" if ws else "DAIS · LIVE"   # show where you are
        head = f" {ident}  {clk} · ↻{self.interval:g}s · {badge}{run}{gates}{cap}"
        if self.filtering:
            head += f"   /{self.filter}_"
        elif self.flash and time.monotonic() < self._flash_until:
            head += f"   — {self.flash}"
        head_attr = (self._cp(7) | curses.A_BOLD) if self.has_color else curses.A_REVERSE
        _add(scr, 0, 0, pad_cols(head, w - 1), w, head_attr)

        rows = self.left_rows()
        sel_i, sel_row = self._selected(rows)
        self.sel_id = sel_row["id"] if sel_row else None
        body_top = 1
        body_h = max(1, h - body_top - 2)
        wide = w >= 80
        split = max(30, w * 2 // 5) if wide else w
        # left pane (scrolls with selection)
        base = max(0, sel_i - body_h + 1) if rows else 0
        view = rows[base:][:body_h] if rows else []
        for idx, r in enumerate(view):
            attr = self._row_attr(r)
            if (base + idx) == sel_i and self.focus == "left" and r.get("sel", True):
                attr |= curses.A_REVERSE
            _add(scr, body_top + idx, 0, pad_cols(r["label"], split - 1), split, attr)
        # right pane (detail)
        if wide:
            dw = w - split - 2
            for y in range(body_top, body_top + body_h):
                _add(scr, y, split - 1, "│", w, curses.A_DIM)
            if sel_row and sel_row.get("kind") == "running":
                # fixed header + a divider/scroll-indicator row, then the live log:
                # follows the newest line by default; scroll up (tab to the pane,
                # then j/k · PgUp/Dn · Home/End) to read history back to line one.
                yy = body_top
                for ln in self.running_header(sel_row, now):
                    if yy >= body_top + body_h:
                        break
                    _add(scr, yy, split + 1, ln, w, self._cp(1) | curses.A_BOLD)
                    yy += 1
                log_h = max(0, body_top + body_h - yy - 1)   # reserve a row for the divider
                disp = self._log_disp(sel_row.get("log_path"), dw)
                total = len(disp)
                start, self.log_top, self.log_follow, max_top = log_window(
                    total, log_h, self.log_top, self.log_follow)
                self._log_h, self._log_total = log_h, total
                hot = (self.focus == "right")               # is this pane taking scroll keys?
                if total == 0:
                    bar = "── live log ── (no output yet)"
                elif self.log_follow:
                    bar = "── live log ── following" + ("  ·  k to scroll back" if hot else "")
                else:
                    bar = (f"── log {start + 1}-{min(start + log_h, total)}/{total} ── "
                           + ("j/k·PgUp/Dn·Home·End=live" if hot else "tab to scroll"))
                _add(scr, yy, split + 1, bar, w, self._cp(3) | curses.A_BOLD)
                yy += 1
                for piece, attr in disp[start:start + log_h]:
                    _add(scr, yy, split + 1, piece, w, attr)
                    yy += 1
            else:
                wrapped = []
                for ln in self.detail_lines(sel_row):
                    if len(ln) <= dw:
                        wrapped.append(ln)
                        continue
                    indent = " " * (len(ln) - len(ln.lstrip()))
                    wrapped.extend(textwrap.wrap(ln, dw, subsequent_indent=indent) or [""])
                self.detail_scroll = max(0, min(self.detail_scroll, max(0, len(wrapped) - 1)))
                shown = wrapped[self.detail_scroll:self.detail_scroll + body_h]
                for idx, ln in enumerate(shown):
                    attr = self._line_attr(ln)
                    if self.focus == "right" and idx == 0:
                        attr |= curses.A_REVERSE
                    _add(scr, body_top + idx, split + 1, ln, w, attr)
        sind = scroll_indicator(base, len(view), len(rows))
        parked = " · b hide-parked" if self.show_parked else " · b parked"
        nav = f" j/k move · tab pane · g group · / filter{parked} · l log · q quit"
        if sind:
            nav += f"   {sind}"
        # second footer line: the contextual action bar for the selected row
        bar = " " + self.action_bar(sel_row)
        _add(scr, h - 2, 0, pad_cols(nav, w - 1), w, curses.A_REVERSE)
        _add(scr, h - 1, 0, pad_cols(bar, w - 1), w, curses.A_REVERSE | curses.A_BOLD)
        scr.refresh()

    # ---- input ----
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
        url = row and row["task"] and row["task"].pr_url
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

    def deploy_project(self, proj):
        """Founder-gate (always manual): run the project's `deploy:` command. Outward → confirm first,
        then launch detached so it streams to the run log + shows in RUNNING. If the un-deployed
        commits include a MIGRATION, say so loudly and run the `deploy_migrate:` path (the migration
        was already founder-merged + pre-flighted by now); if there's no deploy_migrate: for it, stop
        and point at the runbook rather than deploy code ahead of an un-run migration."""
        if not proj:
            self.flash = "select a project to deploy"
            return
        if not project_field(self.root, proj, "deploy"):
            self.flash = f"{proj} has no deploy: command (set one in project.yaml)"
            return
        p = next((x for x in (self.snap.projects if self.snap else []) if x.name == proj), None)
        mig = bool(p and getattr(p, "deploy_migration", False))
        if mig and not project_field(self.root, proj, "deploy_migrate"):
            self.flash = f"{proj}: pending deploy includes a MIGRATION but no deploy_migrate: set — deploy via the runbook"
            return
        msg = (f"deploy {proj} — ⚠ includes a DB MIGRATION (pre-flight done?)" if mig
               else f"deploy {proj}? (this goes LIVE)")
        if not self._confirm(msg):
            return
        cmd = ["deploy", proj, "--migrate"] if mig else ["deploy", proj]
        if self._spawn_agent(cmd):
            self.flash = (f"deploying {proj} (migration) …" if mig else f"deploying {proj} …")

    def file_deploy_fix(self, proj):
        """File a task to fix the project's last FAILED deploy (its error + log path). Deliberate, not
        automatic — deploy failures are often transient/ops, so this is founder-initiated. Lands in
        backlog (high) for you to triage: promote if it's a build break, cancel if it was a blip."""
        if not proj:
            self.flash = "select a project"
            return
        row = self.conn.execute(
            "SELECT summary, log_path FROM runs WHERE project=? AND agent='deploy' "
            "AND status='failed' ORDER BY id DESC LIMIT 1", (proj,)).fetchone()
        if not row:
            self.flash = f"no failed deploy for {proj}"
            return
        if not self._confirm(f"file a fix task for {proj}'s failed deploy?"):
            return
        notes = (f"Last deploy FAILED: {row['summary'] or '(no summary)'}. "
                 f"Log: {row['log_path'] or '(none)'}. Triage: ops/transient (retry with D?) vs a real "
                 f"build break (then fix). Filed by founder from the panel.")
        rc = self._dispatch(["task", "add", proj, f"deploy failed: {proj} — investigate",
                             "--priority", "high", "--status", "backlog", "--notes", notes])
        self.refresh()
        self.flash = (f"filed deploy-fix task in {proj}" if rc == 0 else f"add failed (exit {rc})")

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

    def _row_actions(self, row):
        """task_actions for a row (the single source the bar, menu and keys share)."""
        t = self._task_of(row)
        if t is None:
            return []
        return task_actions(t["status"], self._row_kind(row),
                            has_pr=bool(t.get("pr_url")))

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

    def _ship_pr(self, row, cmd):
        """ship is a real merge that streams output → drop out of curses, run it,
        pause for the founder to read, then restore. The curses/`input` dance only
        runs on a real tty (so the dispatch is unit-testable headless)."""
        tid = (self._task_of(row) or {}).get("id")
        live = sys.stdout.isatty()
        if live:
            try:
                curses.def_prog_mode()
                curses.endwin()
            except curses.error:
                pass
            print(f"\n=== dais {' '.join(str(c) for c in cmd)}  ({tid}) ===\n", flush=True)
        rc = self._dispatch(cmd)
        if live:
            try:
                input(f"\n[ship exited {rc}] press enter to return to dais top… ")
            except (EOFError, KeyboardInterrupt):
                pass
            try:
                curses.reset_prog_mode()
            except curses.error:
                pass
            self.scr.clear()
            self.scr.refresh()
        return rc

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
        cmd = action_command(action_id, t)
        if cmd is not None:
            act = next((a for a in self._row_actions(row) if a.id == action_id), None)
            if act and act.confirm:
                if not self._confirm(f"{BAR_LABEL.get(action_id, action_id)} {t['id']}?"):
                    return
            if action_id == "ship":
                rc = self._ship_pr(row, cmd)
                self.refresh()
                self.flash = (f"shipped {t['id']}" if rc == 0
                              else f"ship: {t['id']} NOT merged (exit {rc}) — see output")
                return
            if action_id == "start":          # launches a streaming agent — background it (never over curses)
                if self._spawn_agent(cmd):
                    self.flash = f"started {t['id']} — see RUNNING / press L for the log"
                self.refresh()
                return
            rc = self._dispatch(cmd)
            self.refresh()
            self.flash = (f"{BAR_LABEL.get(action_id, action_id)} {t['id']}" if rc == 0
                          else f"{action_id} failed (exit {rc})")
            return
        # cmd is None → interactive action
        if action_id == "set_priority":
            self._priority_menu(row)
        elif action_id == "handoff":
            self._handoff(row)
        elif action_id == "edit_title":
            self._edit_title(row)
        elif action_id == "open_pr":
            self.launch_pr(row)
        else:
            self.flash = f"no handler for {action_id}"

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

    def _handoff(self, row):
        t = self._task_of(row)
        if not t or not t.get("id"):
            return
        roles = [r for r in project_roles(self.root, t["project"]) if r != "founder"]
        idx = self._menu(f"handoff {t['id']} to which role?", roles)
        if idx is None:
            return
        rc = self._dispatch(["handoff", t["id"], roles[idx]])
        self.refresh()
        self.flash = (f"{t['id']} → {roles[idx]}" if rc == 0
                      else f"handoff failed (exit {rc})")

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
        on_log = self.focus == "right" and bool(sel_row and sel_row.get("kind") == "running")
        page = max(1, self._log_h - 1)
        if ch in (ord("j"), curses.KEY_DOWN):
            if on_log:
                self.log_top += 1                # draw() re-pins to the live tail past the end
            elif self.focus == "right":
                self.detail_scroll += 1
            elif rows:
                self.sel_id = self._move_sel(rows, sel_i, +1)
                self.detail_scroll = 0
                self._reset_log_scroll()
        elif ch in (ord("k"), curses.KEY_UP):
            if on_log:
                self.log_follow = False
                self.log_top = max(0, self.log_top - 1)
            elif self.focus == "right":
                self.detail_scroll = max(0, self.detail_scroll - 1)
            elif rows:
                self.sel_id = self._move_sel(rows, sel_i, -1)
                self.detail_scroll = 0
                self._reset_log_scroll()
        elif ch == curses.KEY_NPAGE:         # PageDown
            if on_log:
                self.log_top += page
            elif self.focus == "right":
                self.detail_scroll += page
        elif ch == curses.KEY_PPAGE:         # PageUp
            if on_log:
                self.log_follow = False
                self.log_top = max(0, self.log_top - page)
            elif self.focus == "right":
                self.detail_scroll = max(0, self.detail_scroll - page)
        elif ch == curses.KEY_HOME:          # jump to first line
            if on_log:
                self.log_follow, self.log_top = False, 0
            elif self.focus == "right":
                self.detail_scroll = 0
        elif ch == curses.KEY_END:           # snap back to the live tail
            if on_log:
                self._reset_log_scroll()
        elif ch in (9,):                 # tab
            self.focus = "right" if self.focus == "left" else "left"
        elif ch in (10, 13):             # enter — expand a project, else the action menu
            if sel_row and sel_row["kind"] == "project":
                self.expanded ^= {sel_row["project"]}
            elif sel_row:
                self._action_menu(sel_row)
        elif ch == ord("g"):
            self.mode = "queue" if self.mode == "project" else "project"
            self.detail_scroll = 0
            self._reset_log_scroll()
        elif ch == ord("l"):
            self.launch_logs(sel_row)
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
        elif ch == ord("D"):                 # deploy the selected row's project (founder gate)
            self.deploy_project(sel_row["project"] if sel_row else None)
        elif ch == ord("F"):                 # file a fix task for the project's last FAILED deploy
            self.file_deploy_fix(sel_row["project"] if sel_row else None)
        elif 32 <= ch < 127 and (act := next(
                (a for a in self._row_actions(sel_row) if a.key == chr(ch)), None)):
            self.do_action(act.id, sel_row)  # any contextual action by its key (a/x/o/s/h/e …)
        elif ch in (ord("+"), ord("=")):     # raise priority ('=' is the unshifted '+')
            self._bump_priority(sel_row, +1)
        elif ch == ord("-"):                 # lower priority
            self._bump_priority(sel_row, -1)
        elif ch == ord("n"):                 # new task in the row's project
            self.do_action("new", sel_row)
        elif ch == ord("b"):                 # reveal/hide founder-parked rows
            self.show_parked = not self.show_parked
            self.flash = ("showing parked (backlog+deferred)" if self.show_parked
                          else "hiding parked")
        elif ch == ord("/"):
            self.filtering = True
            self.filter = ""
        elif ch == ord("r"):
            self.refresh()
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
        if os.environ.get("DAIS_CLASSIC"):       # opt back into the classic single-pane UI
            curses.wrapper(lambda scr: App(scr, args.interval).run())
        else:                                    # the control panel is the default `dais top`
            import panel
            curses.wrapper(lambda scr: panel.PanelApp(scr, args.interval).run())
        return 0
    with connect() as conn:
        print(render_plain(load_snapshot(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
