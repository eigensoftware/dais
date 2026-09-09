#!/usr/bin/env python3
"""Notifications (plan 4.4). dais.yaml `notify: <shell command>` receives ONE message on stdin
(DAIS_HOME in its environment). The dispatcher runs `sweep` every real tick and sends once per
arrival: a task newly parked in NEEDS YOU (keyed task|state, so a bounce that comes back is a
new arrival), a task newly held over budget, and a spent daily budget once per day. What was
already announced lives in projects/.notified (the CURRENT set is rewritten each sweep).

    notify.py sweep <DAIS_HOME>            -> prints one line per message sent
    notify.py send  <DAIS_HOME> <message>  -> exit 1 when no notify: command is configured
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import machine as MC   # noqa: E402
import router          # noqa: E402


def command(root):
    try:
        with open(os.path.join(root, "dais.yaml")) as fh:
            return router._yaml_line(fh.read(), "notify")
    except OSError:
        return ""


def send(root, text):
    cmd = command(root)
    if not cmd:
        return 1
    try:
        return subprocess.run(cmd, shell=True, input=text + "\n", text=True, timeout=30,
                              env=dict(os.environ, DAIS_HOME=root)).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def _projects(root):
    pdir = os.path.join(root, "projects")
    out = []
    for d in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        py = os.path.join(pdir, d, "project.yaml")
        if not os.path.exists(py):
            continue
        with open(py) as fh:
            if router._yaml_line(fh.read(), "archived") == "true":
                continue
        out.append(d)
    return out


def current(root, conn, now=None):
    """{key: message} for everything that should have been announced as of now."""
    out = {}
    for p in _projects(root):
        m = MC.load(MC.project_machine_path(root, p))
        # keyed on the ARRIVAL (state_entered_at, 0011): a task that bounces out and back between
        # two ticks is a new arrival, not the one already announced
        have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        stamp = "state_entered_at" if "state_entered_at" in have else "updated_at"
        rows = conn.execute("SELECT id, title, status, COALESCE(%s,'') FROM tasks WHERE project=? "
                            "AND status NOT IN ('done','cancelled')" % stamp, (p,)).fetchall()
        for tid, title, st, at in rows:
            if MC.band_of(m, st) == "NEEDS YOU":
                out["%s|%s|%s" % (tid, st, at)] = "◆ %s · %s [%s] %s — dais brief %s" % (p, tid, st.replace("_", " "), title, tid)
        for tid, ob in router.over_budget_tasks(root, p, conn=conn).items():
            out["%s|over-budget" % tid] = ("⛔ %s · %s over budget (%d runs) — withheld; lift: dais task set %s --budget-lift"
                                          % (p, tid, ob["runs"], tid))
    b = router.daily_budget_state(root, now=now, conn=conn)
    if b and b["over"]:
        day = (now or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))[:10]
        out["budget|%s" % day] = "⛔ daily budget spent (%s/%s %s) — the loop idles until tomorrow" % (
            b["spent"], b["limit"], "usd" if b["unit"] == "usd" else "tokens")
    return out


def sweep(root, now=None):
    if not command(root):
        return []
    conn = MC.open_db(os.path.join(root, "dais.db"))
    cur = current(root, conn, now=now)
    marker = os.path.join(root, "projects", ".notified")
    try:
        with open(marker) as fh:
            seen = {l.strip() for l in fh if l.strip()}
    except OSError:
        seen = set()
    sent = []
    for key in sorted(k for k in cur if k not in seen):
        if send(root, cur[key]) == 0:
            sent.append(key)
    try:
        with open(marker, "w") as fh:                      # the CURRENT set: a task that leaves and
            fh.write("\n".join(sorted(cur)) + ("\n" if cur else ""))   # comes back is a new arrival
    except OSError:
        pass
    return sent


def _main(argv):
    if len(argv) >= 2 and argv[0] == "sweep":
        for k in sweep(argv[1]):
            print("notified %s" % k)
        return 0
    if len(argv) >= 3 and argv[0] == "send":
        if not command(argv[1]):
            print("notify: no `notify:` command in %s/dais.yaml — e.g. notify: cat >> $DAIS_HOME/notify.log"
                  % argv[1], file=sys.stderr)
            return 1
        rc = send(argv[1], " ".join(argv[2:]))
        print("sent" if rc == 0 else "notify: the command exited %d" % rc)
        return 0 if rc == 0 else 1
    print("usage: notify.py sweep <DAIS_HOME> | send <DAIS_HOME> <message>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
