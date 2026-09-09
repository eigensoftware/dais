#!/usr/bin/env python3
"""`dais cost` — the run ledger's report (migration 0008: runs.input_tokens … session_id).

Tokens are the primary unit: both providers report them and they compare across providers.
Dollars appear where the provider reported them (claude's total_cost_usd — the API-equivalent
even on a subscription; codex reports none, so codex-only rows show no figure). A no-op run
is the dispatcher's definition: succeeded, and no run_tasks verb other than 'touch' — the
runs the harness-side idle check (plan 1.5) is meant to remove.

    cost.py <db> [project] [--since Nd] [--by project|role|task|account]
"""
import sqlite3
import sys


def _k(n):
    """Compact token count: 812 · 7.1k · 210k · 1.20M."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 10000:
        return "%.1fk" % (n / 1000.0)
    if n < 1000000:
        return "%.0fk" % (n / 1000.0)
    return "%.2fM" % (n / 1000000.0)


def _usd(v):
    return "—" if v is None else "$%.2f" % v


def _pct(part, whole):
    return "—" if not whole else "%d%%" % round(100.0 * (part or 0) / whole)


def report(conn, project=None, since_days=None, by="project", now=None):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "input_tokens" not in cols:
        return ("dais cost: this db has no run ledger yet — run `dais migrate` (with the loop paused); "
                "usage is recorded from the next run on")
    have_rt = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_tasks'").fetchone())
    now = now or conn.execute("SELECT datetime('now')").fetchone()[0]

    where, args = ["1=1"], []
    if project:
        where.append("r.project=?"); args.append(project)
    if since_days:
        where.append("r.started_at > datetime(?, ?)"); args += [now, "-%d days" % int(since_days)]
    W = " AND ".join(where)
    noop = ("(r.status='succeeded' AND NOT EXISTS(SELECT 1 FROM run_tasks rt "
            "WHERE rt.run_id=r.id AND rt.verb<>'touch'))" if have_rt
            else "(r.status='succeeded' AND COALESCE(r.summary,'no task changes')='no task changes')")
    agg = ("COUNT(*) runs, SUM(r.input_tokens) tin, SUM(r.cache_read_tokens) cached, "
           "SUM(r.output_tokens) tout, SUM(r.cost_usd) cost, SUM(r.cost_usd IS NOT NULL) priced, "
           "SUM(%s) noop, SUM(r.input_tokens IS NULL) unreported" % noop)

    tot = conn.execute("SELECT %s FROM runs r WHERE %s" % (agg, W), args).fetchone()
    scope = (project or "all projects") + " · " + ("last %dd" % int(since_days) if since_days else "all time")
    head = ("dais cost — %s · %d runs · %s in (%s cached) · %s out · %s%s"
            % (scope, tot["runs"], _k(tot["tin"]), _pct(tot["cached"], tot["tin"]), _k(tot["tout"]),
               _usd(tot["cost"] if tot["priced"] else None),
               "" if (tot["priced"] or 0) == tot["runs"] else " (priced runs only; codex reports no dollars)"))
    if tot["unreported"]:
        head += " · %d run%s reported no usage" % (tot["unreported"], "" if tot["unreported"] == 1 else "s")
    out = [head, ""]

    if by == "task":
        if not have_rt:
            out.append("  (by task needs run_tasks — run `dais migrate`)")
            return "\n".join(out)
        rows = conn.execute(
            "SELECT x.task_id tid, COALESCE(t.title,'') title, COUNT(*) runs, SUM(r.input_tokens) tin, "
            "SUM(r.output_tokens) tout, SUM(r.cost_usd) cost, SUM(r.cost_usd IS NOT NULL) priced "
            "FROM (SELECT DISTINCT run_id, task_id FROM run_tasks) x JOIN runs r ON r.id=x.run_id "
            "LEFT JOIN tasks t ON t.id=x.task_id WHERE %s GROUP BY x.task_id "
            "ORDER BY tin DESC, x.task_id" % W, args).fetchall()
        out.append("  %-12s %-8s %-9s %-8s %-8s title" % ("task", "runs", "in", "out", "cost"))
        for r in rows:
            out.append("  %-12s %-8s %-9s %-8s %-8s %s" % (
                r["tid"], "%d runs" % r["runs"], _k(r["tin"]) + " in", _k(r["tout"]) + " out",
                _usd(r["cost"] if r["priced"] else None), r["title"][:60]))
        return "\n".join(out)

    if by == "account":                          # 5.4: runs.account (0015); NULL = the provider's implicit account
        if "account" not in cols:
            return "  (by account needs runs.account — run `dais migrate`)"
        key, label = "COALESCE(r.account, r.provider, 'anthropic')", "account"
    else:
        key = "r.project" if by == "project" else "r.project || '/' || r.agent"
        label = "project" if by == "project" else "project/role"
    rows = conn.execute("SELECT %s k, %s FROM runs r WHERE %s GROUP BY k ORDER BY tin DESC, k"
                        % (key, agg, W), args).fetchall()
    out.append("  %-18s %5s %8s %7s %8s %8s %-12s %s" % (label, "runs", "in", "cached", "out", "cost", "no-op", "avg in/run"))
    for r in rows:
        priced_cost = r["cost"] if r["priced"] else None
        reported = r["runs"] - (r["unreported"] or 0)
        avg = _k((r["tin"] or 0) / reported) if reported else "—"
        out.append("  %-18s %5d %8s %7s %8s %8s %-12s %s" % (
            r["k"], r["runs"], _k(r["tin"]), _pct(r["cached"], r["tin"]), _k(r["tout"]),
            _usd(priced_cost), "%d no-op (%s)" % (r["noop"] or 0, _pct(r["noop"], r["runs"])), avg))
    return "\n".join(out)


def _main(argv):
    if not argv:
        print("usage: cost.py <db> [project] [--since Nd] [--by project|role|task|account]", file=sys.stderr)
        return 2
    db, project, since, by = argv[0], None, None, "project"
    rest = argv[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--since":
            v = rest[i + 1].rstrip("d")
            if not v.isdigit():
                print("cost: --since wants a day count like 7d", file=sys.stderr); return 1
            since = int(v); i += 2
        elif a == "--by":
            by = rest[i + 1]
            if by not in ("project", "role", "task", "account"):
                print("cost: --by project|role|task|account", file=sys.stderr); return 1
            i += 2
        elif a.startswith("--"):
            print("cost: unknown flag %s" % a, file=sys.stderr); return 1
        else:
            project = a; i += 1
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    print(report(conn, project=project, since_days=since, by=by))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
