#!/usr/bin/env python3
"""`dais web [port]` — a localhost, token-gated page over the SAME data layer as the panel
(board.load_snapshot), with actions that go through `dais fire` so the engine enforces every
guard exactly as the CLI does (plan 4.5). Stdlib only: http.server + a single HTML file.

Routes (all under /<token>/; a wrong token is a 404 everywhere):
  GET  /                      the page (harness/web.html)
  GET  /api/snapshot          the board as JSON (projects, bands, tasks, runs, gates, cooling, budget)
  GET  /api/events            Server-Sent Events: a snapshot frame every 2s
  GET  /api/edges/<task>      the fireable edges + what each needs (machine.prompts_for)
  GET  /api/brief/<task>      the decision packet (text)
  GET  /api/machine/<project> states, edges, live counts, bands
  GET  /api/cost?by=&since=   the ledger report (text) · GET /api/retro?since= the retro (text)
  GET  /api/series?days=14    the charts' data: daily tokens by role, the 24h run timeline, gate stats, tiles
  POST /api/fire              {task, verb, confirm?, typed?, attest?[], verify?[], notes?, verdict?}
  POST /api/task              {task, notes? | priority? | title? | pr? | budget_lift?}
  POST /api/loop              {action: pause | resume}

Bind 127.0.0.1 only. Phone access is a Tailscale command, not code.
"""
import dataclasses
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HARNESS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HARNESS)
sys.path.insert(0, HARNESS)
import dashboard as d      # noqa: E402  (board.load_snapshot + gate_count/running_threads/watch_state)
import machine as MC       # noqa: E402
import brief               # noqa: E402
import cost as costmod     # noqa: E402
import retro as retromod   # noqa: E402

SSE_INTERVAL = 2.0


# --------------------------------------------------------------------------- data
def _conn(root):
    return MC.open_db(os.path.join(root, "dais.db"))


def snapshot_json(root):
    conn = _conn(root)
    snap = d.load_snapshot(conn, root=root)
    projects = []
    for p in snap.projects:
        m = p.machine or {}
        projects.append({
            "name": p.name, "stage_goal": p.stage_goal,
            "running": [{"agent": a, "since": s, "run_id": r} for a, s, r in (p.running or [])],
            "tasks_by_status": {st: [dataclasses.asdict(t) for t in ts] for st, ts in p.tasks_by_status.items()},
            "bands": MC.bands(m) if m else {},
            "machine": {"name": m.get("name"), "states": m.get("states", {}), "edges": m.get("edges", []),
                        "checks": list((m.get("checks") or {}).keys())},
            "recent_runs": [dataclasses.asdict(r) for r in p.recent_runs],
            "last_tick": p.last_tick, "pending_learnings": getattr(p, "pending_learnings", 0),
        })
    running_ids = {t.get("run_id") for t in d.running_threads(snap, root=root)}
    state, interval, par = d.watch_state(root)
    return {
        "ts": snap.ts, "workspace": snap.workspace, "projects": projects,
        "recent_runs": [dataclasses.asdict(r) for r in snap.recent_runs],
        "gates": d.gate_count(snap, running_ids), "cooling": snap.cooling, "budget": snap.budget,
        "last_tick": snap.last_tick, "watch": {"state": state, "interval": interval, "parallel": par},
        "over_budget": [{"project": pn, "task": t.id, **(t.over_budget or {})} for pn, t in d.over_budget_tasks_in(snap)],
    }


def edges_json(root, tid):
    conn = _conn(root)
    task = MC.task_row(conn, tid)
    if task is None:
        return None
    m = MC.load(MC.project_machine_path(root, task["project"]))
    out = []
    for e in MC.edges_from(m, task["status"]):
        if e.get("by") == "system":
            continue
        eff = e.get("effect") or {}
        out.append({"verb": e["verb"], "to": e["to"], "by": e.get("by"), "guards": e.get("guards", []),
                    "human": e.get("by") == "founder", "prompts": MC.prompts_for(m, e, task),
                    "effects": {k: v for k, v in eff.items()}})
    out.sort(key=lambda x: 0 if x["human"] else 1)
    return {"task": tid, "state": task["status"], "project": task["project"],
            "dispatch": MC.dispatch_role(m, task["status"]), "edges": out}


def machine_json(root, project):
    mp = MC.project_machine_path(root, project)
    if not os.path.exists(mp):
        return None
    m = MC.load(mp)
    conn = _conn(root)
    counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks WHERE project=? GROUP BY status",
                                                (project,))}
    return {"name": m.get("name"), "states": m.get("states", {}), "edges": m.get("edges", []),
            "counts": counts, "bands": MC.bands(m), "roles": m.get("roles", {})}


def series_json(root, days=14):
    """The charts' data (plan 4.6): daily prompt tokens by role (top 3 roles + Other), the
    last 24h of runs as a timeline, the founder gates' stats, and the stat tiles."""
    conn = _conn(root)
    days = max(1, min(int(days or 14), 90))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    dates = [time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400)) for i in range(days - 1, -1, -1)]
    by_role = {}
    try:
        rows = conn.execute("SELECT agent, date(started_at) d, COALESCE(SUM(input_tokens),0) t FROM runs "
                            "WHERE date(started_at) >= ? GROUP BY agent, d", (dates[0],)).fetchall()
    except Exception:
        rows = []
    for agent, dday, t in rows:
        by_role.setdefault(agent, {})[dday] = t
    totals = sorted(by_role, key=lambda a: -sum(by_role[a].values()))
    top, other = totals[:3], totals[3:]
    spend = {a: [by_role[a].get(dd, 0) for dd in dates] for a in top}
    if other:
        spend["other"] = [sum(by_role[a].get(dd, 0) for a in other) for dd in dates]
    try:
        runs = [dict(r) for r in conn.execute(
            "SELECT id, project, agent, status, started_at, ended_at, input_tokens FROM runs "
            "WHERE started_at > datetime('now','-24 hours') ORDER BY started_at").fetchall()]
    except Exception:
        runs = []
    gates = retromod.gate_stats(conn, since_days=30)
    try:
        tt = conn.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), SUM(cost_usd) FROM runs "
                          "WHERE date(started_at)=date('now')").fetchone()
    except Exception:
        tt = (0, 0, None)
    snap = d.load_snapshot(conn, root=root)
    waits = [g["wait_median_s"] for g in gates if g["wait_median_s"] is not None]
    return {"days": dates, "today": today, "spend_by_role": spend, "runs": runs, "gates": gates,
            "tiles": {"tokens_today": tt[1], "cost_today": tt[2], "runs_today": tt[0],
                      "gates_waiting": d.gate_count(snap),
                      "median_wait_s": (sorted(waits)[len(waits) // 2] if waits else None)}}


# --------------------------------------------------------------------------- actions
def _dais(root, *args):
    r = subprocess.run([os.path.join(ROOT, "dais"), *args], capture_output=True, text=True,
                       env=dict(os.environ, DAIS_HOME=root, NO_COLOR="1"), timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def fire(root, body):
    tid, verb = body.get("task"), body.get("verb")
    if not tid or not verb:
        return 400, {"error": "task and verb are required"}
    args = ["fire", tid, verb]
    if body.get("confirm"):
        args.append("--confirm")
    if body.get("typed"):
        args += ["--typed", str(body["typed"])]
    for fact in body.get("attest") or []:
        args += ["--attest", str(fact)]
    for check in body.get("verify") or []:
        args += ["--verify", str(check)]
    if body.get("notes"):
        args += ["--notes", str(body["notes"])]
    if body.get("verdict"):
        args += ["--verdict", json.dumps(body["verdict"]) if isinstance(body["verdict"], dict) else str(body["verdict"])]
    rc, out = _dais(root, *args)
    return (200, {"ok": True, "output": out}) if rc == 0 else (409, {"error": out or "refused"})


def task_set(root, body):
    tid = body.get("task")
    if not tid:
        return 400, {"error": "task is required"}
    args = ["task", "set", tid]
    if body.get("notes"):
        args += ["--notes", str(body["notes"])]
    if body.get("priority"):
        if body["priority"] not in MC.PRIORITY_ORDER:
            return 400, {"error": "priority must be one of %s" % ", ".join(MC.PRIORITY_ORDER)}
        args += ["--priority", body["priority"]]
    if body.get("title"):
        args += ["--title", str(body["title"])]
    if body.get("pr"):
        args += ["--pr", str(body["pr"])]
    if body.get("budget_lift"):
        args.append("--budget-lift")
    if len(args) == 3:
        return 400, {"error": "nothing to set"}
    rc, out = _dais(root, *args)
    return (200, {"ok": True, "output": out}) if rc == 0 else (400, {"error": out})


def loop(root, body):
    action = body.get("action")
    if action not in ("pause", "resume"):
        return 400, {"error": "action must be pause or resume"}
    rc, out = _dais(root, action)
    return (200, {"ok": True, "output": out}) if rc == 0 else (400, {"error": out})


# --------------------------------------------------------------------------- server
def make_server(root, host, port, token):
    with open(os.path.join(HARNESS, "web.html"), encoding="utf-8") as fh:
        page = fh.read()

    class Handler(BaseHTTPRequestHandler):
        server_version = "dais-web"

        def log_message(self, fmt, *args):      # quiet; the tick journal is the record that matters
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else (json.dumps(body) if ctype.startswith("application/json")
                                                         else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _route(self):
            u = urlsplit(self.path)
            parts = [unquote(x) for x in u.path.split("/") if x]
            if not parts or parts[0] != token:
                return None, None, {}
            return "/" + "/".join(parts[1:]), parts[1:], parse_qs(u.query)

        def do_GET(self):
            path, parts, qs = self._route()
            if path is None:
                return self._send(404, {"error": "not found"})
            try:
                if path == "/":
                    return self._send(200, page, "text/html; charset=utf-8")
                if path == "/api/snapshot":
                    return self._send(200, snapshot_json(root))
                if path == "/api/events":
                    return self._sse()
                if len(parts) == 3 and parts[1] == "edges":
                    e = edges_json(root, parts[2])
                    return self._send(200, e) if e else self._send(404, {"error": "no task"})
                if len(parts) == 3 and parts[1] == "brief":
                    return self._send(200, brief.render(_conn(root), root, parts[2]), "text/plain; charset=utf-8")
                if len(parts) == 3 and parts[1] == "machine":
                    m = machine_json(root, parts[2])
                    return self._send(200, m) if m else self._send(404, {"error": "no project"})
                if path == "/api/series":
                    dd = qs.get("days", ["14"])[0]
                    return self._send(200, series_json(root, int(dd) if dd.isdigit() else 14))
                if path == "/api/cost":
                    since = qs.get("since", [""])[0].rstrip("d")
                    return self._send(200, costmod.report(_conn(root), by=qs.get("by", ["project"])[0],
                                                          since_days=int(since) if since.isdigit() else None),
                                      "text/plain; charset=utf-8")
                if path == "/api/retro":
                    since = qs.get("since", ["30"])[0].rstrip("d")
                    return self._send(200, retromod.report(_conn(root), since_days=int(since) if since.isdigit() else 30),
                                      "text/plain; charset=utf-8")
            except Exception as ex:      # a data error must not kill the page: report it
                return self._send(500, {"error": str(ex)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path, parts, _qs = self._route()
            if path is None:
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
            except ValueError as ex:
                return self._send(400, {"error": "bad JSON: %s" % ex})
            routes = {"/api/fire": fire, "/api/task": task_set, "/api/loop": loop}
            fn = routes.get(path)
            if not fn:
                return self._send(404, {"error": "not found"})
            code, out = fn(root, body)
            return self._send(code, out)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    frame = "data: " + json.dumps(snapshot_json(root)) + "\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(SSE_INTERVAL)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def main(argv):
    port = int(argv[0]) if argv and argv[0].isdigit() else 8791
    root = os.environ.get("DAIS_HOME") or ROOT
    token = secrets.token_urlsafe(12)
    srv = make_server(root, "127.0.0.1", port, token)
    print("dais web — %s" % root, flush=True)
    print("  open:  http://127.0.0.1:%d/%s/" % (srv.server_address[1], token), flush=True)
    print("  phone: tailscale serve %d   (then the same path on your tailnet name)" % srv.server_address[1], flush=True)
    print("  Ctrl-C to stop", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
