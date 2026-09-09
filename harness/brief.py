#!/usr/bin/env python3
"""`dais brief <task>` — the decision packet for a founder gate, on one screen (plan 4.1).

What the founder assembled by hand before deciding: the task and how long it has waited, the
work a release encompasses (each with its PR, the QA verdict, the recorded checks, its last
note), the migrations flag the greenlight will demand, the proposal's spec and what it has
cost so far, a suspected duplicate, an escalated task's bounce history — and then exactly
what can be fired from here, with its guards and effects. PR facts (size, mergeable) come
from `gh` when it is on PATH; otherwise the brief says so and renders the rest.

    brief.py <db> <DAIS_HOME> <task-id>
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import machine as MC   # noqa: E402
from cost import _k as _tokens   # noqa: E402


def _age(ts, now):
    try:
        a = time.mktime(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"))
        b = time.mktime(time.strptime(now[:19], "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return "?"
    m = max(0, int((b - a) // 60))
    if m < 60:
        return "%dm" % m
    if m < 24 * 60:
        return "%dh" % (m // 60)
    return "%dd" % (m // (24 * 60))


def _entries(notes):
    return [e.strip() for e in (notes or "").split("\n\n") if e.strip()]


def _verdict_line(task):
    try:
        v = json.loads(task.get("verdict") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(v, dict):
        return None
    line = "%s" % (v.get("verdict") or v.get("verb") or "?")
    if v.get("summary"):
        line += " — %s" % v["summary"]
    if v.get("by"):
        line += " (%s)" % v["by"]
    return line


def _checks_line(task, now):
    try:
        rec = json.loads(task.get("check_results") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or not rec:
        return None
    parts = []
    for name, r in rec.items():
        mark = "✓" if r.get("ok") else "✗"
        parts.append("%s %s (%s ago)" % (name, mark, _age(r.get("at", ""), now)))
    return ", ".join(parts)


def _pr_facts(pr):
    """'+120 −8 in 6 files · OPEN · MERGEABLE' via gh, or None when gh is absent or fails."""
    if not pr or not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(["gh", "pr", "view", pr, "--json", "state,mergeable,additions,deletions,changedFiles"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        d = json.loads(out.stdout)
        return "+%s −%s in %s files · %s · %s" % (d.get("additions", "?"), d.get("deletions", "?"),
                                                  d.get("changedFiles", "?"), d.get("state", "?"),
                                                  d.get("mergeable", "?"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _spend(conn, tid):
    try:
        r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(r.input_tokens),0) t, SUM(r.cost_usd) c, "
                         "SUM(r.cost_usd IS NOT NULL) priced FROM (SELECT DISTINCT run_id FROM run_tasks "
                         "WHERE task_id=?) x JOIN runs r ON r.id=x.run_id", (tid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not r or not r["n"]:
        return None
    line = "%d run%s · %s in" % (r["n"], "" if r["n"] == 1 else "s", _tokens(r["t"]))
    if r["priced"]:
        line += " · $%.2f" % (r["c"] or 0)
    return line


def _row(conn, tid):
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return dict(r) if r is not None else None


def render(conn, root, tid, now=None):
    now = now or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    task = _row(conn, tid)
    if task is None:
        return "no task: %s" % tid
    project = task["project"]
    m = MC.load(MC.project_machine_path(root, project))
    state = task["status"]
    out = []
    P = out.append
    P("▌ %s — %s" % (tid, task["title"]))
    since = task.get("state_entered_at") or task.get("updated_at") or ""
    band = MC.band_of(m, state)
    P("  %s · %s · waiting %s%s" % (state.replace("_", " "), band.lower(),
                                    _age(since, now), (" (since %s)" % since[:16]) if since else ""))
    if task.get("priority"):
        P("  priority %s · assignee %s" % (task["priority"], task.get("assignee") or "-"))
    need_gh = False

    # --- what a release encompasses --------------------------------------------------------
    kids = [r[0] for r in conn.execute("SELECT child_id FROM task_links WHERE parent_id=? AND rel='encompasses' "
                                       "ORDER BY id", (tid,)).fetchall()]
    if kids:
        P("")
        P("  encompasses %d task%s:" % (len(kids), "" if len(kids) == 1 else "s"))
        for cid in kids:
            c = _row(conn, cid)
            if not c:
                continue
            P("   • %s  %s  [%s]" % (cid, c["title"], c["status"].replace("_", " ")))
            if c.get("pr_url"):
                facts = _pr_facts(c["pr_url"])
                if facts is None and c.get("pr_url"):
                    need_gh = True
                P("       PR %s%s" % (c["pr_url"], ("  " + facts) if facts else ""))
            v = _verdict_line(c)
            P("       verdict: %s" % (v if v else "no verdict recorded"))
            ck = _checks_line(c, now)
            if ck:
                P("       checks: %s" % ck)
            ents = _entries(c.get("notes"))
            if ents:
                last = ents[-1].replace("\n", " ")
                P("       last note: %s" % (last[:140] + ("…" if len(last) > 140 else "")))
        tm = task.get("touches_migrations")
        if tm is None:
            P("  migrations: unknown — the greenlight still demands attest:migration_reviewed (set: dais task set %s --touches-migrations true|false)" % tid)
        elif tm:
            P("  migrations: yes — the greenlight demands attest:migration_reviewed")
        else:
            P("  migrations: no — the migrations attest is lifted")

    # --- the task's own PR, verdict, checks -------------------------------------------------
    if task.get("pr_url") and not kids:
        facts = _pr_facts(task["pr_url"])
        if facts is None:
            need_gh = True
        P("  PR %s%s" % (task["pr_url"], ("  " + facts) if facts else ""))
    v = _verdict_line(task)
    if v:
        P("  latest verdict: %s" % v)
    ck = _checks_line(task, now)
    if ck:
        P("  checks: %s" % ck)

    # --- spend, duplicates, budget ----------------------------------------------------------
    sp = _spend(conn, tid)
    if sp:
        P("  spend so far: %s" % sp)
    dups = [e for e in _entries(task.get("notes")) if "possible duplicate" in e]
    for d in dups:
        P("  ⚠ %s" % d.split("] ", 1)[-1])

    # --- the notes log (the whole log for a proposal / an escalation; the tail otherwise) ----
    ents = _entries(task.get("notes"))
    if ents:
        show = ents if state in ("proposal_review", "proposed", "escalated", "note") else ents[-3:]
        P("")
        P("  notes (%d entr%s%s):" % (len(ents), "y" if len(ents) == 1 else "ies",
                                      "" if len(show) == len(ents) else ", last %d" % len(show)))
        for e in show:
            for i, line in enumerate(e.split("\n")):
                P("   %s %s" % ("│" if i else "•", line))

    # --- what can be fired from here --------------------------------------------------------
    P("")
    P("  next — fire one (dais fire %s <verb> …):" % tid)
    edges = MC.edges_from(m, state)
    edges.sort(key=lambda e: 0 if e.get("by") == "founder" else 1)
    for e in edges:
        if e.get("by") == "system":
            continue
        g = " {%s}" % ", ".join(e.get("guards", [])) if e.get("guards") else ""
        eff = e.get("effect") or {}
        fx = []
        if "spawn" in eff:
            for sp in MC.spawn_specs(eff):       # one spec or a fan-out list (6.1)
                fx.append("spawns a %s task at %s%s" % (sp.get("template", "task"), sp.get("initial"),
                                                       (" (after %s)" % sp["after"]) if sp.get("after") else ""))
        if "aggregate" in eff:
            fx.append("sweeps the %s pool" % (eff["aggregate"].get("select", "").split("=")[-1] or "approved"))
        if "then" in eff:
            fx.append("then %s" % eff["then"])
        if "script" in eff and isinstance(eff["script"], dict) and eff["script"].get("outward"):
            fx.append("OUTWARD")
        P("   %-16s → %-16s by %-9s%s%s" % (e["verb"], e["to"], e.get("by"), g,
                                             ("  · " + "; ".join(fx)) if fx else ""))
    if need_gh:
        P("")
        P("  (PR size/mergeable need `gh` on PATH)")
    return "\n".join(out)


def _main(argv):
    if len(argv) < 3:
        print("usage: brief.py <db> <DAIS_HOME> <task-id>", file=sys.stderr)
        return 2
    conn = sqlite3.connect(argv[0], timeout=10)
    conn.row_factory = sqlite3.Row
    print(render(conn, argv[1], argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
