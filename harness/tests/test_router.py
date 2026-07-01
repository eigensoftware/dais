"""Router (scheduler) tests — decide() picks the next role UNCONDITIONALLY from the project's machine
(coding default here): the dispatch role of the top pending task, skipping blocked/parked states and
tasks waiting on an open dependency (tasks.blocked_on). Cadence roles still run on their clock."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import router  # harness/router.py

# Role names must match the machine's dispatch roles (coding: lead/engineer/qa). `handles` is unused
# under machine dispatch — the machine's edges own state->role — but the roles file still supplies the
# cast + cadence. qa+engineer only (no cadence lead) isolates the dependency skip from cadence.
ROLES_REACTIVE = (
    "qa        review  reactive  -  1\n"
    "engineer  edit    reactive  -  2\n"
)
ROLES_WITH_LEAD = ROLES_REACTIVE + "lead  draft  every:5h  -  3\n"

SCHEMA = (
    "CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT, title TEXT, status TEXT,"
    " priority TEXT DEFAULT 'medium'%s);\n"
    "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT,"
    " started_at TEXT, ended_at TEXT, status TEXT);\n"
)


def _ws(tasks, roles=ROLES_REACTIVE, with_dep_col=True):
    """tasks: list of (id, status[, blocked_on[, priority]]). No machine.json/project.yaml, so the
    project runs the coding default machine. Builds a temp workspace + dais.db; returns root."""
    root = tempfile.mkdtemp(prefix="dais-rt-")
    os.makedirs(os.path.join(root, "projects", "p"))
    with open(os.path.join(root, "projects", "p", "roles"), "w") as f:
        f.write(roles)
    conn = sqlite3.connect(os.path.join(root, "dais.db"))
    conn.executescript(SCHEMA % (", blocked_on TEXT" if with_dep_col else ""))
    for t in tasks:
        dep = t[2] if len(t) > 2 else None
        pri = t[3] if len(t) > 3 else "medium"
        if with_dep_col:
            conn.execute("INSERT INTO tasks(id,project,title,status,blocked_on,priority) VALUES(?,?,?,?,?,?)",
                         (t[0], "p", t[0], t[1], dep, pri))
        else:
            conn.execute("INSERT INTO tasks(id,project,title,status,priority) VALUES(?,?,?,?,?)",
                         (t[0], "p", t[0], t[1], pri))
    conn.commit(); conn.close()
    return root


class TestMachineDispatch(unittest.TestCase):
    def test_ready_task_schedules_engineer(self):
        self.assertEqual(router.decide(_ws([("a", "ready")]), "p"), "engineer")

    def test_proposed_task_schedules_lead(self):
        # proposed dispatches the lead (machine edge), reactively — not via cadence.
        self.assertEqual(router.decide(_ws([("a", "proposed")]), "p"), "lead")

    def test_qa_review_task_schedules_qa(self):
        self.assertEqual(router.decide(_ws([("a", "qa_review")]), "p"), "qa")

    def test_parked_state_does_not_dispatch(self):
        # awaiting_release/blocked have no dispatch role → nothing to run, no cadence → idle.
        self.assertIsNone(router.decide(_ws([("a", "awaiting_release")]), "p"))

    def test_higher_priority_task_wins(self):
        # dispatch is priority-ordered (not role-precedence): a HIGH ready outranks a medium proposal.
        root = _ws([("a", "proposed"), ("b", "ready", None, "high")])
        self.assertEqual(router.decide(root, "p"), "engineer")


class TestDependencySkip(unittest.TestCase):
    def test_blocked_task_is_not_scheduled(self):
        # a (ready) waits on b (awaiting_release → not done); it's the only dispatchable task, so
        # decide idles instead of running on the blocked task.
        self.assertIsNone(router.decide(_ws([("a", "ready", "b"), ("b", "awaiting_release")]), "p"))

    def test_unblocked_when_predecessor_done(self):
        self.assertEqual(router.decide(_ws([("a", "ready", "b"), ("b", "done")]), "p"), "engineer")

    def test_unblocked_when_predecessor_cancelled(self):
        self.assertEqual(router.decide(_ws([("a", "ready", "b"), ("b", "cancelled")]), "p"), "engineer")

    def test_dangling_dependency_is_not_blocked(self):
        # predecessor doesn't exist (deleted) → treat as unblocked so work is never stranded.
        self.assertEqual(router.decide(_ws([("a", "ready", "ghost")]), "p"), "engineer")

    def test_degrades_without_blocked_on_column(self):
        # a pre-migration DB (no blocked_on column) must still schedule, not crash.
        self.assertEqual(router.decide(_ws([("a", "ready")], with_dep_col=False), "p"), "engineer")


class TestCadence(unittest.TestCase):
    def test_lead_cadence_runs_for_discovery_when_idle(self):
        # no dispatchable reactive work → the lead still runs on its cadence (first run, never-run).
        self.assertEqual(router.decide(_ws([("a", "awaiting_release")], roles=ROLES_WITH_LEAD), "p"), "lead")

    def test_blocked_work_falls_through_to_cadence_not_reactive(self):
        # a proposed task blocked on an open predecessor is NOT reactive; with no cadence lead it idles
        # (proving the blocked task itself didn't trigger a dispatch).
        self.assertIsNone(router.decide(_ws([("a", "proposed", "b"), ("b", "awaiting_release")]), "p"))


if __name__ == "__main__":
    unittest.main()
