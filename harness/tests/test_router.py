"""Router (scheduler) tests — decide() picks the next role UNCONDITIONALLY from the project's machine
(coding default here): the dispatch role of the top pending task, skipping blocked/parked states and
tasks waiting on an open dependency (tasks.blocked_on). Cadence roles still run on their clock."""
import os
import shutil
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
        # approved/blocked have no dispatch role → nothing to run, no cadence → idle.
        self.assertIsNone(router.decide(_ws([("a", "approved")]), "p"))

    def test_higher_priority_task_wins(self):
        # dispatch is priority-ordered (not role-precedence): a HIGH ready outranks a medium proposal.
        root = _ws([("a", "proposed"), ("b", "ready", None, "high")])
        self.assertEqual(router.decide(root, "p"), "engineer")


class TestDependencySkip(unittest.TestCase):
    def test_blocked_task_is_not_scheduled(self):
        # a (ready) waits on b (approved → not done); it's the only dispatchable task, so
        # decide idles instead of running on the blocked task.
        self.assertIsNone(router.decide(_ws([("a", "ready", "b"), ("b", "approved")]), "p"))

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


class TestTriggerNone(unittest.TestCase):
    def test_trigger_none_gates_machine_dispatch(self):
        # a DORMANT role (trigger=none, e.g. a shelved project's lead) must never be scheduled,
        # even when the machine's edges would dispatch it — none means never scheduled.
        roles = ("engineer  edit  reactive  -  2\nlead  draft  none  -  3\n")
        self.assertIsNone(router.decide(_ws([("a", "proposed")], roles=roles), "p"))

    def test_reactive_role_still_dispatches(self):
        roles = ("engineer  edit  reactive  -  2\nlead  draft  reactive  -  3\n")
        self.assertEqual(router.decide(_ws([("a", "proposed")], roles=roles), "p"), "lead")

    def test_cadence_role_is_still_reactively_dispatchable(self):
        # every:Nh marks cadence, not dormancy — the machine may still dispatch it reactively
        roles = ("engineer  edit  reactive  -  2\nlead  draft  every:5h  -  3\n")
        self.assertEqual(router.decide(_ws([("a", "proposed")], roles=roles), "p"), "lead")


class TestExcludedRoles(unittest.TestCase):
    """decide(root, project, excluded={...}) skips tasks whose dispatch role is excluded and keeps
    scanning — so a throttled lead doesn't starve the engineer's ready work behind it. Cadence
    honors the exclusion too."""

    def test_excluded_top_role_falls_through_to_next_task(self):
        # proposed (high -> lead) outranks ready (med -> engineer); excluding lead surfaces engineer
        root = _ws([("a", "proposed", None, "high"), ("b", "ready", None, "medium")])
        self.assertEqual(router.decide(root, "p"), "lead")
        self.assertEqual(router.decide(root, "p", excluded={"lead"}), "engineer")

    def test_excluded_only_role_idles(self):
        root = _ws([("a", "proposed")])
        self.assertIsNone(router.decide(root, "p", excluded={"lead"}))

    def test_cadence_honors_exclusion(self):
        # no reactive work; the cadence lead would run — unless excluded
        root = _ws([("a", "approved")], roles=ROLES_WITH_LEAD)
        self.assertEqual(router.decide(root, "p"), "lead")
        self.assertIsNone(router.decide(root, "p", excluded={"lead"}))


class TestCadence(unittest.TestCase):
    def test_lead_cadence_runs_for_discovery_when_idle(self):
        # no dispatchable reactive work → the lead still runs on its cadence (first run, never-run).
        self.assertEqual(router.decide(_ws([("a", "approved")], roles=ROLES_WITH_LEAD), "p"), "lead")

    def test_blocked_work_falls_through_to_cadence_not_reactive(self):
        # a proposed task blocked on an open predecessor is NOT reactive; with no cadence lead it idles
        # (proving the blocked task itself didn't trigger a dispatch).
        self.assertIsNone(router.decide(_ws([("a", "proposed", "b"), ("b", "approved")]), "p"))


class TestFrontmatter(unittest.TestCase):
    """Flat `key: value` lines between leading --- markers of a persona file.
    Line-based on purpose (no YAML library) — nested values are not supported."""
    def _write(self, text):
        d = tempfile.mkdtemp(prefix="dais-fm-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "qa.md")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_reads_flat_keys(self):
        p = self._write("---\nmodel: claude-opus-4-8[1m]\ntrigger: every:5h\nprec: 3\n---\nYou are QA.\n")
        fm = router.frontmatter(p)
        self.assertEqual(fm["model"], "claude-opus-4-8[1m]")
        self.assertEqual(fm["trigger"], "every:5h")   # value itself may contain ':'
        self.assertEqual(fm["prec"], "3")

    def test_inline_comment_stripped(self):
        p = self._write("---\neffort: high   # crank it\n---\nbody\n")
        self.assertEqual(router.frontmatter(p)["effort"], "high")

    def test_no_frontmatter_is_empty(self):
        self.assertEqual(router.frontmatter(self._write("You are QA. No block here.\n")), {})

    def test_unterminated_block_is_empty(self):
        self.assertEqual(router.frontmatter(self._write("---\nmodel: x\nno closing marker\n")), {})

    def test_missing_file_is_empty(self):
        self.assertEqual(router.frontmatter("/nonexistent/qa.md"), {})

    def test_blank_and_comment_lines_ignored(self):
        p = self._write("---\n\n# a comment\nplaybook: plan\n---\nbody\n")
        self.assertEqual(router.frontmatter(p), {"playbook": "plan"})


if __name__ == "__main__":
    unittest.main()
