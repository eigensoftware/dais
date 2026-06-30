"""Router (scheduler) tests — decide() picks the next role from the board state, and must SKIP a
task that's blocked on an unfinished predecessor (task dependencies, #6-B)."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import router  # harness/router.py

# qa + engineer only (no cadence lead) so decide() returns None when the only work is blocked,
# isolating the dependency skip from cadence scheduling.
ROLES_REACTIVE = (
    "qa        review  reactive  needs_qa                 1\n"
    "engineer  edit    reactive  changes_requested,ready  2\n"
)

# qa + engineer + a CADENCE lead that also `handles needs_scoping` — the real-world shape.
ROLES_WITH_LEAD = ROLES_REACTIVE + "lead  review  every:5h  needs_scoping  3\n"

SCHEMA = (
    "CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT, title TEXT, status TEXT,"
    " priority TEXT DEFAULT 'medium'%s);\n"
    "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT,"
    " started_at TEXT, ended_at TEXT, status TEXT);\n"
)


def _ws(tasks, roles=ROLES_REACTIVE, with_dep_col=True):
    """tasks: list of (id, status[, blocked_on]). Builds a temp workspace + dais.db; returns root."""
    root = tempfile.mkdtemp(prefix="dais-rt-")
    os.makedirs(os.path.join(root, "projects", "p"))
    with open(os.path.join(root, "projects", "p", "roles"), "w") as f:
        f.write(roles)
    conn = sqlite3.connect(os.path.join(root, "dais.db"))
    conn.executescript(SCHEMA % (", blocked_on TEXT" if with_dep_col else ""))
    for t in tasks:
        dep = t[2] if len(t) > 2 else None
        if with_dep_col:
            conn.execute("INSERT INTO tasks(id,project,title,status,blocked_on) VALUES(?,?,?,?,?)",
                         (t[0], "p", t[0], t[1], dep))
        else:
            conn.execute("INSERT INTO tasks(id,project,title,status) VALUES(?,?,?,?)",
                         (t[0], "p", t[0], t[1]))
    conn.commit(); conn.close()
    return root


class TestDependencySkip(unittest.TestCase):
    def test_ready_task_schedules_engineer(self):
        self.assertEqual(router.decide(_ws([("a", "ready")]), "p"), "engineer")

    def test_blocked_task_is_not_scheduled(self):
        # a (ready) is blocked on b (still backlog → not done); it's the only ready task, so the
        # engineer has NO actionable work and decide idles instead of running on the blocked task.
        self.assertIsNone(router.decide(_ws([("a", "ready", "b"), ("b", "backlog")]), "p"))

    def test_unblocked_when_predecessor_done(self):
        # b is done → a is no longer blocked → the engineer runs.
        self.assertEqual(router.decide(_ws([("a", "ready", "b"), ("b", "done")]), "p"), "engineer")

    def test_unblocked_when_predecessor_cancelled(self):
        self.assertEqual(router.decide(_ws([("a", "ready", "b"), ("b", "cancelled")]), "p"), "engineer")

    def test_dangling_dependency_is_not_blocked(self):
        # predecessor doesn't exist (deleted) → treat as unblocked so work is never stranded.
        self.assertEqual(router.decide(_ws([("a", "ready", "ghost")]), "p"), "engineer")

    def test_degrades_without_blocked_on_column(self):
        # a pre-migration DB (no blocked_on column) must still schedule, not crash.
        self.assertEqual(router.decide(_ws([("a", "ready")], with_dep_col=False), "p"), "engineer")


class TestLeadReactiveOnScoping(unittest.TestCase):
    """A cadence lead that `handles needs_scoping` reacts to it on the next tick (not just on its 5h
    clock), while still keeping its periodic discovery run when no reactive work is pending."""

    def test_lead_reacts_to_needs_scoping(self):
        # a needs_scoping task with no qa/eng work → the lead is scheduled reactively this tick.
        root = _ws([("a", "needs_scoping")], roles=ROLES_WITH_LEAD)
        self.assertEqual(router.decide(root, "p"), "lead")

    def test_builders_outrank_scoping(self):
        # verify→build→plan: a ready task (engineer, prec 2) wins over scoping (lead, prec 3).
        root = _ws([("a", "ready"), ("b", "needs_scoping")], roles=ROLES_WITH_LEAD)
        self.assertEqual(router.decide(root, "p"), "engineer")

    def test_lead_cadence_runs_for_discovery_when_idle(self):
        # no reactive work at all → the lead still runs on its cadence (first run, never-run) for
        # its own feature discovery / backlog re-ranking.
        root = _ws([("a", "backlog")], roles=ROLES_WITH_LEAD)
        self.assertEqual(router.decide(root, "p"), "lead")

    def test_blocked_scoping_does_not_trigger_lead(self):
        # the lone needs_scoping task is blocked → no reactive work; falls through to cadence (still
        # the lead here, but via the cadence path) — proving the blocked task itself didn't trigger it.
        root = _ws([("a", "needs_scoping", "b"), ("b", "backlog")], roles=ROLES_WITH_LEAD)
        # with a prior lead run recorded, cadence wouldn't fire; assert the blocked task alone is
        # not reactive by checking a no-lead roles set idles.
        self.assertIsNone(router.decide(_ws([("a", "needs_scoping", "b"), ("b", "backlog")],
                                            roles=ROLES_REACTIVE + "lead review reactive needs_scoping 3\n"), "p"))


if __name__ == "__main__":
    unittest.main()
