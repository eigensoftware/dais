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
        self.assertEqual(M.dispatch_role(m, "design"), "designer")  # the design lane
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


class TestPendingFor(unittest.TestCase):
    """pending_for — next_role's scan as a count; the role-concurrency gate reads it."""
    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def test_counts_dispatchable_tasks_for_role(self):
        self.conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('a','proj','x','ready')")
        self.conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('b','proj','y','ready')")
        self.conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('c','proj','z','qa_review')")
        self.conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('d','proj','w','approved')")
        self.assertEqual(M.pending_for(self.conn, self.m, "proj", "engineer"), 2)
        self.assertEqual(M.pending_for(self.conn, self.m, "proj", "qa"), 1)
        self.assertEqual(M.pending_for(self.conn, self.m, "proj", "lead"), 0)

    def test_dep_blocked_tasks_do_not_count(self):
        self.conn.execute("ALTER TABLE tasks ADD COLUMN blocked_on TEXT")  # migration 0001
        self.conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('open','proj','x','doing')")
        self.conn.execute("INSERT INTO tasks(id,project,title,status,blocked_on) "
                          "VALUES('kid','proj','y','ready','open')")
        self.assertEqual(M.pending_for(self.conn, self.m, "proj", "engineer"), 1)  # 'doing' only


class TestAssigneeStamp(unittest.TestCase):
    """The first AGENT to fire an edge on an unassigned task stamps itself as assignee, so
    archived tasks record their worker. Explicit assignments are never overwritten; founder
    edges don't claim authorship."""
    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def _row(self, tid):
        return self.conn.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()

    def test_agent_fire_stamps_empty_assignee(self):
        self.conn.execute("INSERT INTO tasks(id,project,title,status) "
                          "VALUES('t1','proj','x','ready')")
        M.fire(self.conn, self.m, "t1", "claim", "engineer")
        self.assertEqual(self._row("t1")["assignee"], "engineer")

    def test_explicit_assignee_is_not_overwritten(self):
        self.conn.execute("INSERT INTO tasks(id,project,title,status,assignee) "
                          "VALUES('t2','proj','x','ready','designer')")
        M.fire(self.conn, self.m, "t2", "claim", "engineer")
        self.assertEqual(self._row("t2")["assignee"], "designer")

    def test_founder_fire_does_not_stamp(self):
        self.conn.execute("INSERT INTO tasks(id,project,title,status) "
                          "VALUES('t3','proj','x','ready')")
        M.fire(self.conn, self.m, "t3", "defer", "founder")
        self.assertFalse((self._row("t3")["assignee"] or "").strip())


class TestHistoryPark(unittest.TestCase):
    """deferred is a HISTORY state: undefer returns a task to wherever it was parked from —
    a proposal back to proposed (never skipping the front gate), a build task back to ready."""
    def setUp(self):
        self.conn = _db()
        self.conn.execute("ALTER TABLE tasks ADD COLUMN parked_from TEXT")  # migration 0004
        self.m = M.load(CODING)

    def test_deferred_proposal_returns_to_proposed(self):
        p = M.create_task(self.conn, self.m, "proj", "someday idea")
        M.fire(self.conn, self.m, p, "defer", "founder")
        self.assertEqual(_status(self.conn, p), "deferred")
        r = M.fire(self.conn, self.m, p, "undefer", "founder")
        self.assertEqual(r["to"], "proposed")
        self.assertEqual(_status(self.conn, p), "proposed")     # scoping still ahead of it

    def test_deferred_ready_task_returns_to_ready(self):
        p = M.create_task(self.conn, self.m, "proj", "scoped work")
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (p,))
        M.fire(self.conn, self.m, p, "defer", "founder")
        M.fire(self.conn, self.m, p, "undefer", "founder")
        self.assertEqual(_status(self.conn, p), "ready")

    def test_legacy_park_falls_back_to_ready(self):
        # rows deferred BEFORE history existed have no parked_from — undefer keeps old behavior
        p = M.create_task(self.conn, self.m, "proj", "old parked task")
        self.conn.execute("UPDATE tasks SET status='deferred', parked_from=NULL WHERE id=?", (p,))
        M.fire(self.conn, self.m, p, "undefer", "founder")
        self.assertEqual(_status(self.conn, p), "ready")

    def test_unmigrated_db_degrades_to_fallback(self):
        conn = _db()                                            # no parked_from column at all
        p = M.create_task(conn, self.m, "proj", "idea")
        M.fire(conn, self.m, p, "defer", "founder")             # stash is a no-op, no crash
        M.fire(conn, self.m, p, "undefer", "founder")
        self.assertEqual(_status(conn, p), "ready")

    def test_lint_rejects_history_target_without_history_state(self):
        m = M.load(CODING)
        m["states"]["deferred"].pop("history")
        errors, _ = M.lint(m)
        self.assertTrue(any("@history" in e for e in errors), errors)


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


class TestConditionalMigrationsAttest(unittest.TestCase):
    """`attest:migrations_applied when task:touches_migrations` — the release greenlight demands the
    migrations attestation only when `assemble` recorded that the release touches migration files. An
    explicit false LIFTS it; true, NULL (unknown), and an unmigrated db all still REQUIRE it — the
    gate is never skipped by omission (fail-safe)."""
    def setUp(self):
        self.conn = _db()
        self.conn.execute("ALTER TABLE tasks ADD COLUMN parked_from TEXT")            # migration 0004
        self.conn.execute("ALTER TABLE tasks ADD COLUMN touches_migrations INTEGER")  # migration 0005
        self.m = M.load(CODING)

    def _assembled_release(self, conn=None):
        conn = conn or self.conn
        rel = M.create_task(conn, self.m, "proj", "release", state="release_open", assignee="engineer")
        M.fire(conn, self.m, rel, "assemble", "engineer")
        return rel

    def test_false_flag_lifts_the_attest(self):
        rel = self._assembled_release()
        self.conn.execute("UPDATE tasks SET touches_migrations=0 WHERE id=?", (rel,))
        M.fire(self.conn, self.m, rel, "greenlight", "founder", {"typed": rel})  # no --attest needed
        self.assertEqual(_status(self.conn, rel), "releasing")

    def test_true_flag_requires_the_attest(self):
        rel = self._assembled_release()
        self.conn.execute("UPDATE tasks SET touches_migrations=1 WHERE id=?", (rel,))
        with self.assertRaises(M.GuardFailure):
            M.fire(self.conn, self.m, rel, "greenlight", "founder", {"typed": rel})
        M.fire(self.conn, self.m, rel, "greenlight", "founder",
               {"typed": rel, "attest": {"migrations_applied": True}})
        self.assertEqual(_status(self.conn, rel), "releasing")

    def test_null_flag_requires_the_attest(self):        # unknown -> still gated
        rel = self._assembled_release()                  # touches_migrations left NULL
        with self.assertRaises(M.GuardFailure):
            M.fire(self.conn, self.m, rel, "greenlight", "founder", {"typed": rel})

    def test_unmigrated_db_requires_the_attest(self):    # column absent -> fail-safe require
        conn = _db()                                     # no touches_migrations column at all
        rel = self._assembled_release(conn)
        with self.assertRaises(M.GuardFailure):
            M.fire(conn, self.m, rel, "greenlight", "founder", {"typed": rel})


class TestAttestFact(unittest.TestCase):
    """attest_fact(guard, task) is the ONE shared reading of an `attest:` guard — the engine
    enforces with it and the panel decides whether to PROMPT with it, so the two layers can
    never disagree on whether a conditional attest is live for a task."""
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks(id TEXT, touches_migrations INTEGER)")
        conn.execute("INSERT INTO tasks VALUES('t-0', 0), ('t-1', 1), ('t-n', NULL)")
        self.row = lambda tid: conn.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    def test_unconditional_attest_always_required(self):
        self.assertEqual(M.attest_fact("attest:migrations_applied", self.row("t-0")),
                         "migrations_applied")

    def test_false_flag_lifts(self):
        self.assertIsNone(M.attest_fact(
            "attest:migrations_applied when task:touches_migrations", self.row("t-0")))

    def test_true_flag_requires(self):
        self.assertEqual(M.attest_fact(
            "attest:migrations_applied when task:touches_migrations", self.row("t-1")),
            "migrations_applied")

    def test_null_flag_requires(self):
        self.assertEqual(M.attest_fact(
            "attest:migrations_applied when task:touches_migrations", self.row("t-n")),
            "migrations_applied")

    def test_missing_column_requires(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks(id TEXT)")
        conn.execute("INSERT INTO tasks VALUES('t-x')")
        bare = conn.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(M.attest_fact(
            "attest:migrations_applied when task:touches_migrations", bare),
            "migrations_applied")

    def test_no_task_row_requires(self):
        self.assertEqual(M.attest_fact(
            "attest:migrations_applied when task:touches_migrations", None),
            "migrations_applied")

    def test_non_attest_guard_is_none(self):
        self.assertIsNone(M.attest_fact("typed_confirm", self.row("t-1")))


class TestPromptsFor(unittest.TestCase):
    """prompts_for(m, edge, task) is the ONE reading of an edge's guard list for elicitation —
    a UI asks it what a human must supply, so the panel can never prompt for something fire()
    won't demand (or skip something it will). Conditional attests resolve against the task row
    with the same rule as enforcement."""
    def setUp(self):
        self.conn = _db()
        self.conn.execute("ALTER TABLE tasks ADD COLUMN parked_from TEXT")
        self.conn.execute("ALTER TABLE tasks ADD COLUMN touches_migrations INTEGER")
        self.m = M.load(CODING)

    def _edge(self, frm, verb):
        return next(e for e in M.edges_from(self.m, frm) if e["verb"] == verb)

    def _release(self, flag):
        rel = M.create_task(self.conn, self.m, "proj", "rel", state="release_open", assignee="engineer")
        M.fire(self.conn, self.m, rel, "assemble", "engineer")
        self.conn.execute("UPDATE tasks SET touches_migrations=? WHERE id=?", (flag, rel))
        return M.task_row(self.conn, rel)

    def test_lifted_attest_is_absent(self):
        ps = M.prompts_for(self.m, self._edge("release_review", "greenlight"), self._release(0))
        self.assertEqual([p["kind"] for p in ps], ["typed"])

    def test_unknown_flag_keeps_the_attest(self):
        ps = M.prompts_for(self.m, self._edge("release_review", "greenlight"), self._release(None))
        self.assertEqual([p["kind"] for p in ps], ["typed", "attest"])
        self.assertEqual(ps[1]["fact"], "migrations_applied")

    def test_confirm_edge(self):
        ps = M.prompts_for(self.m, self._edge("proposal_review", "approve"), None)
        self.assertIn({"kind": "confirm"}, ps)

    def test_undeclared_verify_fails_closed(self):
        ps = M.prompts_for(self.m, self._edge("qa_review", "pass"), None)
        self.assertEqual(ps, [{"kind": "verify", "check": "tests_pass", "declared": False}])

    def test_declared_verify_is_marked_runnable(self):
        m = dict(self.m, checks={"tests_pass": "true"})
        ps = M.prompts_for(m, self._edge("qa_review", "pass"), None)
        self.assertEqual(ps, [{"kind": "verify", "check": "tests_pass", "declared": True}])


class TestSpawnInheritsNotes(unittest.TestCase):
    """A spawn effect copies the parent's notes into the child: the spec the founder approved
    travels to the [impl] build task, and QA-fail findings travel to the spawned fix task —
    the engineer never pulls a bare title (the lead-miner's empty-ready-notes finding)."""
    def setUp(self):
        self.conn = _db()
        self.conn.execute("ALTER TABLE tasks ADD COLUMN notes TEXT")
        self.m = M.load(CODING)

    def test_approve_spawn_carries_the_spec(self):
        p = M.create_task(self.conn, self.m, "proj", "big idea", notes="WHAT: x\nACCEPTANCE: y")
        M.fire(self.conn, self.m, p, "submit", "lead")
        r = M.fire(self.conn, self.m, p, "approve", "founder", {"confirm": True})
        child = r["spawned"][0]["id"]
        notes = self.conn.execute("SELECT notes FROM tasks WHERE id=?", (child,)).fetchone()[0]
        self.assertIn("ACCEPTANCE: y", notes)
        self.assertIn(p, notes)                    # provenance: names the parent

    def test_noteless_parent_spawns_clean(self):
        p = M.create_task(self.conn, self.m, "proj", "bare")
        M.fire(self.conn, self.m, p, "submit", "lead")
        r = M.fire(self.conn, self.m, p, "approve", "founder", {"confirm": True})
        child = r["spawned"][0]["id"]
        notes = self.conn.execute("SELECT notes FROM tasks WHERE id=?", (child,)).fetchone()[0]
        self.assertIn(p, notes or "")              # provenance survives even without a spec

    def test_unmigrated_db_spawns_without_notes(self):
        conn = _db()                               # no notes column at all
        p = M.create_task(conn, self.m, "proj", "idea")
        M.fire(conn, self.m, p, "submit", "lead")
        r = M.fire(conn, self.m, p, "approve", "founder", {"confirm": True})
        self.assertTrue(r["spawned"])              # degrade gracefully, never crash


class TestIdPrefixes(unittest.TestCase):
    """Auto-id prefixes are unique per project: a project KEEPS the prefix it owns (has the
    oldest task using it — ids are identity, history never re-labels), a collision loser
    re-derives, and fresh derivation is 4 chars with word-aware variation (puttflow-web
    prefers put+w over putt, which would read like a puttflow id)."""
    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def _seed(self, project, *ids):
        for tid in ids:
            self.conn.execute("INSERT INTO tasks(id,project,title,status) VALUES(?,?,?,'ready')",
                              (tid, project, tid))

    def test_new_project_gets_four_chars(self):
        tid = M.create_task(self.conn, self.m, "winterbraid", "t")
        self.assertEqual(tid, "wint-1")

    def test_established_prefix_is_kept(self):
        self._seed("winterbraid", "win-1", "win-2")
        self.assertEqual(M.create_task(self.conn, self.m, "winterbraid", "t"), "win-3")

    def test_hyphenated_name_borrows_next_word_initial(self):
        # uniqueness is EXACT-match (founder decision 2026-07-04: dai- vs dais- is fine) —
        # putw is unique against put, so the readable word-initial variant wins.
        self._seed("puttflow", "put-1", "put-2")
        self.assertEqual(M.create_task(self.conn, self.m, "puttflow-web", "t"), "putw-1")

    def test_collision_loser_rederives_but_history_stays(self):
        # puttflow owns put- (oldest); puttflow-web got bumped ids under the old scheme
        self._seed("puttflow", "put-1", "put-2")
        self._seed("puttflow-web", "put-7", "put-8")
        self.assertEqual(M.create_task(self.conn, self.m, "puttflow", "t"), "put-3")
        # numbering continues from the project's task COUNT (2 tasks -> -3), prefix re-derives
        self.assertEqual(M.create_task(self.conn, self.m, "puttflow-web", "t"), "putw-3")

    def test_derived_prefix_avoids_other_projects(self):
        self._seed("dais-site", "dais-1")               # dais-site owns "dais"
        tid = M.create_task(self.conn, self.m, "daisy", "t")
        self.assertFalse(tid.startswith("dais-"))       # daisy must vary, not collide
        self.assertNotEqual(tid.split("-")[0], "dais")

    def test_short_name_uses_whole_name(self):
        self.assertEqual(M.create_task(self.conn, self.m, "app", "t"), "app-1")

    def test_uniqueness_is_exact_match_not_prefix_free(self):
        # founder decision 2026-07-04: dai- vs dais- is acceptable — only EXACT prefix
        # collisions force a variation.
        self._seed("dais-site", "dai-1", "dai-2")
        self.assertEqual(M.create_task(self.conn, self.m, "dais", "t"), "dais-1")


class TestCreateTaskAttributes(unittest.TestCase):
    """create_task speaks the full board vocabulary (explicit id, priority, notes, blocked_on)
    so the CLI's `task add` can delegate to the engine instead of re-implementing entry
    resolution + id allocation + collision retry in bash."""
    def setUp(self):
        self.conn = _db()
        self.m = M.load(CODING)

    def test_explicit_id_is_used_verbatim(self):
        self.assertEqual(M.create_task(self.conn, self.m, "proj", "t", tid="custom-7"),
                         "custom-7")

    def test_explicit_id_collision_is_an_error_not_a_retry(self):
        M.create_task(self.conn, self.m, "proj", "t", tid="dup-1")
        with self.assertRaisesRegex(ValueError, "dup-1"):
            M.create_task(self.conn, self.m, "proj", "t2", tid="dup-1")

    def test_state_defaults_to_the_machine_entry(self):
        tid = M.create_task(self.conn, self.m, "proj", "t")
        self.assertEqual(_status(self.conn, tid), self.m["entry"])

    def test_invalid_state_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "invalid status"):
            M.create_task(self.conn, self.m, "proj", "t", state="nonsense")

    def test_priority_and_notes_written(self):
        self.conn.execute("ALTER TABLE tasks ADD COLUMN notes TEXT")
        tid = M.create_task(self.conn, self.m, "proj", "t", priority="high", notes="why")
        row = self.conn.execute("SELECT priority, notes FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["priority"], row["notes"]), ("high", "why"))

    def test_blocked_on_written_and_predecessor_validated(self):
        self.conn.execute("ALTER TABLE tasks ADD COLUMN blocked_on TEXT")
        pred = M.create_task(self.conn, self.m, "proj", "pred")
        tid = M.create_task(self.conn, self.m, "proj", "t", blocked_on=pred)
        self.assertEqual(self.conn.execute(
            "SELECT blocked_on FROM tasks WHERE id=?", (tid,)).fetchone()[0], pred)
        with self.assertRaisesRegex(ValueError, "no such predecessor"):
            M.create_task(self.conn, self.m, "proj", "t2", blocked_on="ghost-9")

    def test_blocked_on_without_column_asks_for_migrate(self):
        with self.assertRaisesRegex(ValueError, "dais migrate"):   # column arrives by migration 0001
            M.create_task(self.conn, self.m, "proj", "t", blocked_on="x-1")

    def test_empty_assignee_means_unassigned_not_dispatch_default(self):
        # the CLI's `task add` passes assignee through explicitly; '' = leave it NULL
        # (omitting the arg keeps the dispatch-role default for python callers/spawns)
        tid = M.create_task(self.conn, self.m, "proj", "t", assignee="")
        self.assertIsNone(self.conn.execute(
            "SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()[0])


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
        real_task = M.task_row
        def stale(conn, tid):
            row = real_task(conn, tid)
            if tid == t:
                conn.execute("UPDATE tasks SET status='deferred' WHERE id=?", (t,))  # racer wins
                M.task_row = real_task                              # only interpose once
            return row
        M.task_row = stale
        try:
            with self.assertRaises(M.GuardFailure):
                M.fire(self.conn, self.m, t, "claim", "engineer")
        finally:
            M.task_row = real_task
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
