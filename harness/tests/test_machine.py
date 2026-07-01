"""Engine + lint tests for the authored task state machine (harness/machine.py)."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import machine as M  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CODING = os.path.join(ROOT, "harness", "machines", "coding.machine.json")


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT, title TEXT, status TEXT,"
        "  assignee TEXT, priority TEXT DEFAULT 'medium', updated_at TEXT DEFAULT (datetime('now')));"
        "CREATE TABLE task_links(id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id TEXT,"
        "  child_id TEXT, rel TEXT, at TEXT DEFAULT (datetime('now')));")
    return conn


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


class TestLint(unittest.TestCase):
    def test_coding_default_is_coherent(self):
        errors, _ = M.lint(M.load(CODING))
        self.assertEqual(errors, [], f"coding default should be coherent, got: {errors}")

    def test_catches_incoherence(self):
        m = M.load(CODING)
        m["edges"].append({"from": "backlog", "to": "nowhere", "by": "founder", "verb": "typo"})
        m["edges"].append({"from": "qa_review", "to": "doing", "by": "lead", "verb": "meddle"})
        m["edges"] = [e for e in m["edges"] if e["from"] != "blocked"]  # dead-end
        errors, _ = M.lint(m)
        codes = " ".join(errors)
        self.assertIn("E1", codes)   # dangling `to`
        self.assertIn("E2", codes)   # dead-end blocked
        self.assertIn("E3", codes)   # qa_review dispatches qa AND lead

    def test_unguarded_outward_effect_warns_only(self):
        m = M.load(CODING)
        for e in m["edges"]:
            if e["from"] == "release_review" and e["to"] == "releasing":
                e["guards"] = ["confirm"]      # strip the strong human gate
        errors, warns = M.lint(m)
        self.assertEqual(errors, [])                       # never blocks
        self.assertTrue(any("W3" in w for w in warns))     # but warns


class TestDispatch(unittest.TestCase):
    def test_dispatch_roles(self):
        m = M.load(CODING)
        self.assertEqual(M.dispatch_role(m, "proposed"), "lead")
        self.assertEqual(M.dispatch_role(m, "ready"), "engineer")
        self.assertEqual(M.dispatch_role(m, "qa_review"), "qa")
        self.assertIsNone(M.dispatch_role(m, "proposal_review"))  # awaits founder (human)
        self.assertIsNone(M.dispatch_role(m, "done"))             # terminal


class TestNextRole(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def test_dispatches_by_state_and_priority(self):
        self.assertEqual(M.next_role(self.conn, self.m, "proj"), "")    # nothing pending
        M.create_task(self.conn, self.m, "proj", "idea")               # proposed -> lead
        self.assertEqual(M.next_role(self.conn, self.m, "proj"), "lead")
        # a HIGH-priority ready task outranks the medium proposal -> engineer dispatches first
        self.conn.execute("INSERT INTO tasks(id,project,title,status,assignee,priority) VALUES"
                          "('proj-9','proj','urgent','ready','engineer','high')")
        self.conn.commit()
        self.assertEqual(M.next_role(self.conn, self.m, "proj"), "engineer")

    def test_parked_and_gate_states_do_not_dispatch(self):
        for st in ("blocked", "awaiting_release", "proposal_review", "deferred", "done"):
            c = _db()
            c.execute("INSERT INTO tasks(id,project,title,status) VALUES('t','proj','x',?)", (st,))
            self.assertEqual(M.next_role(c, self.m, "proj"), "",
                             f"{st!r} should not auto-dispatch a role")


class TestBandsAndActions(unittest.TestCase):
    def setUp(self):
        self.m = M.load(CODING)

    def test_band_derivation(self):
        self.assertEqual(M.band_of(self.m, "proposed"), "QUEUED")          # lead dispatches
        self.assertEqual(M.band_of(self.m, "ready"), "QUEUED")             # engineer dispatches
        self.assertEqual(M.band_of(self.m, "qa_review"), "QUEUED")         # qa dispatches
        self.assertEqual(M.band_of(self.m, "proposal_review"), "NEEDS YOU")  # founder, no dispatch
        self.assertEqual(M.band_of(self.m, "release_review"), "NEEDS YOU")
        self.assertEqual(M.band_of(self.m, "blocked"), "WAITING")          # system-only
        self.assertEqual(M.band_of(self.m, "awaiting_release"), "WAITING")
        self.assertEqual(M.band_of(self.m, "deferred"), "WAITING")         # explicit override
        self.assertEqual(M.band_of(self.m, "done"), "ARCHIVE")
        self.assertEqual(M.band_of(self.m, "cancelled"), "ARCHIVE")

    def test_bands_are_ordered_and_cover_all_states(self):
        b = M.bands(self.m)
        self.assertEqual(list(b)[:4], M.BAND_ORDER)
        covered = {s for ss in b.values() for s in ss}
        self.assertEqual(covered, set(self.m["states"]))

    def test_edge_actions_from_a_founder_gate(self):
        acts = {a["verb"]: a for a in M.edge_actions(self.m, "proposal_review")}
        self.assertIn("approve", acts)
        self.assertTrue(acts["approve"]["human"])
        self.assertTrue(acts["approve"]["confirm"])          # has confirm/verify guards
        self.assertIn("request_changes", acts)

    def test_edge_actions_include_dispatch_start(self):
        acts = {a["verb"]: a for a in M.edge_actions(self.m, "ready")}
        self.assertIn("__start", acts)
        self.assertTrue(acts["__start"]["dispatch"])
        self.assertEqual(acts["__start"]["by"], "engineer")


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def test_proposal_approval_spawns_impl_task(self):
        p = M.create_task(self.conn, self.m, "proj", "add feature X")
        self.assertEqual(_status(self.conn, p), "proposed")
        M.fire(self.conn, self.m, p, "submit", "lead")
        r = M.fire(self.conn, self.m, p, "approve", "founder",
                   {"confirm": True, "verifiers": {"def_of_ready": True}})
        self.assertEqual(_status(self.conn, p), "done")            # proposal is a planning artifact
        self.assertEqual(len(r["spawned"]), 1)
        impl = r["spawned"][0]["id"]
        self.assertEqual(_status(self.conn, impl), "ready")        # impl work goes to the engineer
        self.assertEqual(r["spawned"][0]["rel"], "spawned_from")

    def test_approve_blocked_without_def_of_ready(self):
        p = M.create_task(self.conn, self.m, "proj", "vague idea")
        M.fire(self.conn, self.m, p, "submit", "lead")
        with self.assertRaises(M.GuardFailure):   # def_of_ready checker returns false
            M.fire(self.conn, self.m, p, "approve", "founder", {"confirm": True})
        self.assertEqual(_status(self.conn, p), "proposal_review")  # unchanged

    def test_wrong_actor_rejected(self):
        p = M.create_task(self.conn, self.m, "proj", "x")
        M.fire(self.conn, self.m, p, "submit", "lead")
        with self.assertRaises(M.GuardFailure):
            M.fire(self.conn, self.m, p, "approve", "engineer",   # only founder may approve
                   {"confirm": True, "verifiers": {"def_of_ready": True}})

    def test_qa_fail_spawns_fix_blocks_parent_then_unblocks(self):
        # get an impl task to qa_review
        impl = M.create_task(self.conn, self.m, "proj", "impl", state="ready", assignee="engineer")
        M.fire(self.conn, self.m, impl, "claim", "engineer")
        M.fire(self.conn, self.m, impl, "complete", "engineer")
        self.assertEqual(_status(self.conn, impl), "qa_review")
        # QA fails -> spawns a fix task, parent parks in blocked
        r = M.fire(self.conn, self.m, impl, "fail", "qa")
        self.assertEqual(_status(self.conn, impl), "blocked")
        fix = r["spawned"][0]["id"]
        self.assertEqual(r["spawned"][0]["rel"], "blocks_parent")
        # parent can't unblock while the fix is open
        self.assertEqual(M.advance_unblocked(self.conn, self.m), [])
        # drive the fix to done, then the parent auto-unblocks back to qa_review
        M.fire(self.conn, self.m, fix, "claim", "engineer")
        M.fire(self.conn, self.m, fix, "complete", "engineer")
        M.fire(self.conn, self.m, fix, "pass", "qa", {"verifiers": {"tests_pass": True}})
        # fix now in awaiting_release (a terminal-enough state? no — must be done/cancelled to clear)
        # so it isn't cleared yet:
        self.assertEqual(M.advance_unblocked(self.conn, self.m), [])
        # release the fix to done via a release task, then parent clears
        rel = M.create_task(self.conn, self.m, "proj", "release", state="release_ready", assignee="engineer")
        M.fire(self.conn, self.m, rel, "assemble", "engineer")
        M.fire(self.conn, self.m, rel, "greenlight", "founder",
               {"typed": rel, "attest": {"migrations_applied": True}})
        M.fire(self.conn, self.m, rel, "shipped", "engineer")
        self.assertEqual(_status(self.conn, fix), "done")
        self.assertEqual(M.advance_unblocked(self.conn, self.m), [impl])
        self.assertEqual(_status(self.conn, impl), "qa_review")

    def test_release_batches_and_closes_encompassed(self):
        # two impl tasks reach awaiting_release
        impls = []
        for i in range(2):
            t = M.create_task(self.conn, self.m, "proj", f"feat{i}", state="ready", assignee="engineer")
            M.fire(self.conn, self.m, t, "claim", "engineer")
            M.fire(self.conn, self.m, t, "complete", "engineer")
            M.fire(self.conn, self.m, t, "pass", "qa", {"verifiers": {"tests_pass": True}})
            self.assertEqual(_status(self.conn, t), "awaiting_release")
            impls.append(t)
        rel = M.create_task(self.conn, self.m, "proj", "release", state="release_ready", assignee="engineer")
        agg = M.fire(self.conn, self.m, rel, "assemble", "engineer")
        self.assertCountEqual(agg["encompassed"], impls)             # batched both
        # deploy needs the strong human guards
        with self.assertRaises(M.GuardFailure):
            M.fire(self.conn, self.m, rel, "greenlight", "founder")  # missing typed_confirm/attest
        M.fire(self.conn, self.m, rel, "greenlight", "founder",
               {"typed": rel, "attest": {"migrations_applied": True}})
        M.fire(self.conn, self.m, rel, "shipped", "engineer")        # effect fires each child's edge
        self.assertEqual(_status(self.conn, rel), "done")
        for t in impls:
            self.assertEqual(_status(self.conn, t), "done")

    def test_release_failure_spawns_rollback(self):
        rel = M.create_task(self.conn, self.m, "proj", "release", state="release_ready", assignee="engineer")
        M.fire(self.conn, self.m, rel, "assemble", "engineer")
        M.fire(self.conn, self.m, rel, "greenlight", "founder",
               {"typed": rel, "attest": {"migrations_applied": True}})
        r = M.fire(self.conn, self.m, rel, "release_error", "system")
        self.assertEqual(_status(self.conn, rel), "release_failed")
        self.assertEqual(r["spawned"][0]["rel"], "blocks_parent")    # a rollback task


if __name__ == "__main__":
    unittest.main()
