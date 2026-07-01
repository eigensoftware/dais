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
        for st in ("blocked", "approved", "proposal_review", "deferred", "done"):
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
        self.assertEqual(M.band_of(self.m, "approved"), "WAITING")
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

    def test_verify_guard_fails_closed_without_assertion_or_checker(self):
        # qa `pass` carries verify:tests_pass; with no --verify and no declared checker
        # the guard fails closed and the task doesn't move.
        t = M.create_task(self.conn, self.m, "proj", "impl", state="qa_review", assignee="qa")
        with self.assertRaises(M.GuardFailure):
            M.fire(self.conn, self.m, t, "pass", "qa")
        self.assertEqual(_status(self.conn, t), "qa_review")  # unchanged

    def test_verify_guard_runs_the_machines_declared_checker(self):
        # a machine `checks` entry turns verify:<name> into a REAL command the engine runs.
        t = M.create_task(self.conn, self.m, "proj", "impl", state="qa_review", assignee="qa")
        self.m["checks"] = {"tests_pass": "false"}          # checker fails -> guard fails
        with self.assertRaises(M.GuardFailure):
            M.fire(self.conn, self.m, t, "pass", "qa")
        self.m["checks"] = {"tests_pass": "true"}           # checker passes -> edge fires
        M.fire(self.conn, self.m, t, "pass", "qa")
        self.assertEqual(_status(self.conn, t), "approved")
        del self.m["checks"]

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
        # fix now in approved (a terminal-enough state? no — must be done/cancelled to clear)
        # so it isn't cleared yet:
        self.assertEqual(M.advance_unblocked(self.conn, self.m), [])
        # release the fix to done via a release task, then parent clears
        rel = M.create_task(self.conn, self.m, "proj", "release", state="release_open", assignee="engineer")
        M.fire(self.conn, self.m, rel, "assemble", "engineer")
        M.fire(self.conn, self.m, rel, "greenlight", "founder",
               {"typed": rel, "attest": {"migrations_applied": True}})
        M.fire(self.conn, self.m, rel, "shipped", "engineer")
        self.assertEqual(_status(self.conn, fix), "done")
        self.assertEqual(M.advance_unblocked(self.conn, self.m), [impl])
        self.assertEqual(_status(self.conn, impl), "qa_review")

    def test_release_batches_and_closes_encompassed(self):
        # two impl tasks reach approved
        impls = []
        for i in range(2):
            t = M.create_task(self.conn, self.m, "proj", f"feat{i}", state="ready", assignee="engineer")
            M.fire(self.conn, self.m, t, "claim", "engineer")
            M.fire(self.conn, self.m, t, "complete", "engineer")
            M.fire(self.conn, self.m, t, "pass", "qa", {"verifiers": {"tests_pass": True}})
            self.assertEqual(_status(self.conn, t), "approved")
            impls.append(t)
        rel = M.create_task(self.conn, self.m, "proj", "release", state="release_open", assignee="engineer")
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
        rel = M.create_task(self.conn, self.m, "proj", "release", state="release_open", assignee="engineer")
        M.fire(self.conn, self.m, rel, "assemble", "engineer")
        M.fire(self.conn, self.m, rel, "greenlight", "founder",
               {"typed": rel, "attest": {"migrations_applied": True}})
        r = M.fire(self.conn, self.m, rel, "release_error", "system")
        self.assertEqual(_status(self.conn, rel), "release_failed")
        self.assertEqual(r["spawned"][0]["rel"], "blocks_parent")    # a rollback task


class TestAtomicityAndConcurrency(unittest.TestCase):
    """fire() is one transaction: the transition + ALL effects commit together or roll back
    together, and the state change is a compare-and-swap so racing fires can't double-apply."""

    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def _release_with_kids(self, n=2):
        kids = [M.create_task(self.conn, self.m, "proj", f"k{i}", state="approved") for i in range(n)]
        rel = M.create_task(self.conn, self.m, "proj", "rel", state="release_open", assignee="engineer")
        M.fire(self.conn, self.m, rel, "assemble", "engineer")
        M.fire(self.conn, self.m, rel, "greenlight", "founder",
               {"typed": rel, "attest": {"migrations_applied": True}})
        return rel, kids

    def test_failing_nested_effect_rolls_back_the_whole_fire(self):
        # guard the child edge (approved --released--> done) so the SECOND kid's nested fire
        # fails mid-effect: the parent transition and the first kid must roll back too.
        rel, kids = self._release_with_kids(2)
        for e in self.m["edges"]:
            if e["from"] == "approved" and e["verb"] == "released":
                e["guards"] = ["unblocked"]
        self.conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('blk','proj','b','ready')")
        self.conn.execute("INSERT INTO task_links(parent_id,child_id,rel) VALUES(?,?,?)",
                          (kids[1], "blk", "blocks_parent"))     # kid2's guard will fail
        with self.assertRaises(M.GuardFailure):
            M.fire(self.conn, self.m, rel, "shipped", "engineer")
        self.assertEqual(_status(self.conn, rel), "releasing")   # parent NOT half-shipped
        self.assertEqual(_status(self.conn, kids[0]), "approved")  # kid1 rolled back too

    def test_stale_state_is_a_cas_failure_not_a_double_apply(self):
        # simulate a concurrent writer: the task moves between fire()'s read and its UPDATE.
        t = M.create_task(self.conn, self.m, "proj", "t", state="ready", assignee="engineer")
        real_task = M._task
        def stale(conn, tid):
            row = real_task(conn, tid)
            if tid == t:
                conn.execute("UPDATE tasks SET status='deferred' WHERE id=?", (t,))  # racer wins
                M._task = real_task                              # only interpose once
            return row
        M._task = stale
        try:
            with self.assertRaises(M.GuardFailure):
                M.fire(self.conn, self.m, t, "claim", "engineer")
        finally:
            M._task = real_task
        # the loser must NOT have applied its transition (in this same-connection simulation
        # the rollback also undoes the simulated racer's write; the point is no double-apply).
        self.assertNotEqual(_status(self.conn, t), "doing")

    def test_reaggregate_after_aborted_release(self):
        # a task swept into a release that gets aborted must be re-aggregatable by the next
        # release — otherwise it strands in `approved` with no path to done.
        rel, kids = self._release_with_kids(1)
        M.fire(self.conn, self.m, rel, "release_error", "system")   # releasing -> release_failed
        M.fire(self.conn, self.m, rel, "give_up", "founder", {"confirm": True})  # -> cancelled
        rel2 = M.create_task(self.conn, self.m, "proj", "rel2", state="release_open", assignee="engineer")
        r = M.fire(self.conn, self.m, rel2, "assemble", "engineer")
        self.assertIn(kids[0], r["encompassed"])


class TestLintGaps(unittest.TestCase):
    def setUp(self):
        self.m = M.load(CODING)

    def test_edge_missing_by_is_reported_not_a_crash(self):
        self.m["edges"].append({"from": "ready", "to": "doing", "verb": "oops"})
        errors, _ = M.lint(self.m)                                # must not raise
        self.assertTrue(any("unknown actor" in e for e in errors))

    def test_bad_entry_is_an_error(self):
        self.m["entry"] = "typo"
        errors, _ = M.lint(self.m)
        self.assertTrue(any("E4" in e and "typo" in e for e in errors))

    def test_duplicate_from_verb_is_an_error(self):
        self.m["edges"].append({"from": "ready", "to": "cancelled", "by": "founder", "verb": "claim"})
        errors, _ = M.lint(self.m)
        self.assertTrue(any("E5" in e for e in errors))

    def test_pool_state_actually_dispatches(self):
        # pool: true = any-of-N dispatch; deterministic pick is roles-declaration order.
        self.m["states"]["ready"]["pool"] = True
        self.m["edges"].append({"from": "ready", "to": "doing", "by": "qa", "verb": "grab"})
        errors, _ = M.lint(self.m)
        self.assertEqual(errors, [])                              # E3 suppressed by pool
        self.assertEqual(M.dispatch_role(self.m, "ready"), "engineer")  # declared before qa


class TestReleaseLane(unittest.TestCase):
    """release_lane(m) -> (open_state, pool_state, lane_states): where a release starts, which
    state its assemble aggregates, and every non-terminal state of the lane — so a UI can offer
    'cut a release' and refuse a second one while one is in flight."""

    def test_coding_machine_lane(self):
        m = M.load(M.default_machine_path())
        open_state, pool, lane = M.release_lane(m)
        self.assertEqual(open_state, "release_open")
        self.assertEqual(pool, "approved")
        self.assertEqual(lane, {"release_open", "release_review", "releasing", "release_failed"})

    def test_machine_without_release_lane(self):
        m = {"states": {"a": {}, "done": {"terminal": True}},
             "edges": [{"from": "a", "verb": "finish", "by": "x", "to": "done"}]}
        self.assertEqual(M.release_lane(m), (None, None, set()))


class TestSystemSweeps(unittest.TestCase):
    """The dispatcher's machine hooks: `advance` (system `unblocked` edges, each tick) and
    `recover` (system `interrupt` edges, orphan-reconcile) — both scoped to ONE project so a
    machine never sweeps another project's same-named states."""

    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def test_advance_is_project_scoped(self):
        # two blocked tasks with no open blockers, in different projects — only the
        # scoped project's task advances.
        a = M.create_task(self.conn, self.m, "alpha", "a", state="blocked")
        b = M.create_task(self.conn, self.m, "beta", "b", state="blocked")
        self.assertEqual(M.advance_unblocked(self.conn, self.m, "alpha"), [a])
        self.assertEqual(_status(self.conn, a), "qa_review")
        self.assertEqual(_status(self.conn, b), "blocked")   # other project untouched

    def test_recover_fires_the_machines_interrupt_edge(self):
        # an orphaned mid-flight task (doing) rewinds via the machine's own system
        # `interrupt` edge — to wherever the machine says work resumes (ready).
        t = M.create_task(self.conn, self.m, "alpha", "t", state="doing")
        self.assertEqual(M.recover_interrupted(self.conn, self.m, "alpha"), [t])
        self.assertEqual(_status(self.conn, t), "ready")

    def test_recover_is_project_scoped_and_skips_settled_states(self):
        other = M.create_task(self.conn, self.m, "beta", "o", state="doing")
        parked = M.create_task(self.conn, self.m, "alpha", "p", state="ready")
        self.assertEqual(M.recover_interrupted(self.conn, self.m, "alpha"), [])
        self.assertEqual(_status(self.conn, other), "doing")   # other project untouched
        self.assertEqual(_status(self.conn, parked), "ready")  # not mid-flight — untouched

    def test_task_id_collision_retries_instead_of_dying(self):
        # simulate the creator race: _new_id hands out an id that gets taken before the
        # INSERT lands — _insert_task must re-derive, not die on the UNIQUE constraint.
        conn = _db(); m = M.load(CODING)
        conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('alp-1','alpha','x','ready')")
        real = M._new_id
        calls = {"n": 0}
        def racy(c, project):
            calls["n"] += 1
            return "alp-1" if calls["n"] == 1 else real(c, project)   # first pick collides
        M._new_id = racy
        try:
            tid = M.create_task(conn, m, "alpha", "y")
        finally:
            M._new_id = real
        self.assertNotEqual(tid, "alp-1")
        self.assertEqual(_status(conn, tid), "proposed")

    def test_advance_skips_tasks_with_open_blockers(self):
        parent = M.create_task(self.conn, self.m, "alpha", "p", state="blocked")
        child = M.create_task(self.conn, self.m, "alpha", "fix", state="ready")
        self.conn.execute("INSERT INTO task_links(parent_id,child_id,rel) VALUES(?,?,?)",
                          (parent, child, "blocks_parent"))
        self.assertEqual(M.advance_unblocked(self.conn, self.m, "alpha"), [])
        self.assertEqual(_status(self.conn, parent), "blocked")  # guard held it back, no error


if __name__ == "__main__":
    unittest.main()
