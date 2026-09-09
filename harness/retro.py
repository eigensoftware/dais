#!/usr/bin/env python3
"""`dais retro` — the founder's loop, measured (plan 4.2), from the transition log (0014).

Which gates you decide, how often without changes, how long they waited on you; QA's pass /
fail rate; tasks that bounced; what shipped. And the calibration question that decides yolo:
a gate you approve unchanged ≥ 90% of the time over ≥ 10 decisions is a yolo candidate.

    retro.py <db> [--since Nd]
"""
import sqlite3
import sys
import time

CHANGE_VERBS = {"request_changes", "reject", "abort", "give_up", "cancel", "defer"}


def _t(ts):
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _dur(secs):
    if secs is None:
        return "?"
    m = int(secs // 60)
    if m < 60:
        return "%dm" % m
    if m < 48 * 60:
        return "%dh" % (m // 60)
    return "%dd" % (m // (24 * 60))


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def report(conn, now=None, since_days=30):
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "task_events" not in have:
        return ("dais retro: this db has no transition log yet — run `dais migrate` (with the loop paused); "
                "decisions are recorded from the next fire on")
    now = now or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(_t(now) - since_days * 86400))
    rows = conn.execute("SELECT task_id, project, verb, from_state, to_state, actor, at FROM task_events "
                        "ORDER BY id").fetchall()
    allev = [dict(r) for r in rows]
    ev = [e for e in allev if (e["at"] or "") >= cutoff]
    out = ["dais retro — last %dd · %d transitions" % (since_days, len(ev)), ""]
    P = out.append

    # --- founder gates: decisions, approval share, median wait --------------------------------
    gates = {}
    for e in ev:
        if e["actor"] != "founder":
            continue
        key = (e["project"], e["from_state"])
        # wait = since the task ARRIVED in from_state: the latest earlier event on this task
        # whose to_state is that state (any actor; the whole log, not just the window)
        arrive = None
        for a in allev:
            if a["task_id"] == e["task_id"] and a["to_state"] == e["from_state"] and (a["at"] or "") <= (e["at"] or ""):
                arrive = a["at"]
        wait = (_t(e["at"]) - _t(arrive)) if (arrive and _t(e["at"]) is not None and _t(arrive) is not None) else None
        g = gates.setdefault(key, {"n": 0, "ok": 0, "waits": []})
        g["n"] += 1
        g["ok"] += 0 if e["verb"] in CHANGE_VERBS else 1
        g["waits"].append(wait)
    P("  founder gates (project · state · decisions · approved unchanged · median wait on you)")
    cands = []
    for (proj, st), g in sorted(gates.items()):
        pct = int(round(100.0 * g["ok"] / g["n"])) if g["n"] else 0
        P("    %-12s %-18s %3d decisions · %3d%% approved · median wait %s"
          % (proj, st, g["n"], pct, _dur(_median(g["waits"]))))
        if g["n"] >= 10 and pct >= 90:
            cands.append("%s: %s (%d%% of %d)" % (proj, st, pct, g["n"]))
    if not gates:
        P("    (no founder decisions in the window)")
    P("")
    if cands:
        P("  yolo candidate%s — approved unchanged ≥90%% over ≥10 decisions; auto-approve with `dais yolo <project> on`:"
          % ("" if len(cands) == 1 else "s"))
        for c in cands:
            P("    ⚡ %s" % c)
        P("")

    # --- QA: pass / fail per project, and bounces ------------------------------------------
    qa = {}
    fails = {}
    for e in ev:
        if e["verb"] in ("pass", "fail") and e["actor"] not in ("founder", "system"):
            q = qa.setdefault(e["project"], {"pass": 0, "fail": 0})
            q[e["verb"]] += 1
            if e["verb"] == "fail":
                fails[e["task_id"]] = fails.get(e["task_id"], 0) + 1
    P("  QA (project · pass · fail)")
    for proj, q in sorted(qa.items()):
        tot = q["pass"] + q["fail"]
        P("    %-12s %d pass · %d fail (%d%% fail)" % (proj, q["pass"], q["fail"], int(round(100.0 * q["fail"] / tot)) if tot else 0))
    if not qa:
        P("    (no QA verdicts in the window)")
    bounced = sorted((t, n) for t, n in fails.items() if n >= 2)
    if bounced:
        P("  bounced (≥2 fails): " + ", ".join("%s: %d fails" % (t, n) for t, n in bounced))
    P("")

    # --- throughput ---------------------------------------------------------------------------
    shipped = sum(1 for e in ev if e["verb"] == "shipped")
    done = sum(1 for e in ev if e["to_state"] == "done" and e["verb"] != "shipped")
    P("  throughput: %d release%s shipped · %d task%s closed" % (shipped, "" if shipped == 1 else "s",
                                                                 done, "" if done == 1 else "s"))
    return "\n".join(out)


def _main(argv):
    if not argv:
        print("usage: retro.py <db> [--since Nd]", file=sys.stderr); return 2
    since = 30
    if "--since" in argv:
        v = argv[argv.index("--since") + 1].rstrip("d")
        if not v.isdigit():
            print("retro: --since wants a day count like 30d", file=sys.stderr); return 1
        since = int(v)
    conn = sqlite3.connect(argv[0], timeout=10)
    conn.row_factory = sqlite3.Row
    print(report(conn, since_days=since))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
