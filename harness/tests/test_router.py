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
    " task_id TEXT, started_at TEXT, ended_at TEXT, status TEXT);\n"
)


def _pin(root, agent, task_id, status="running"):
    """Record a run holding `task_id` — what run-agent.sh writes to runs.task_id at startup."""
    conn = sqlite3.connect(os.path.join(root, "dais.db"))
    conn.execute("INSERT INTO runs(project,agent,task_id,status) VALUES('p',?,?,?)",
                 (agent, task_id, status))
    conn.commit(); conn.close()


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


def _with_frontmatter(root, role, fm_lines):
    """Write projects/p/agents/<role>.md with the given frontmatter lines."""
    adir = os.path.join(root, "projects", "p", "agents")
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, role + ".md"), "w") as f:
        f.write("---\n" + "\n".join(fm_lines) + "\n---\n# " + role + "\n")


class TestConcurrencyStacking(unittest.TestCase):
    """decide(live=...) — a busy project may only launch MORE OF THE LIVE ROLE, gated on
    frontmatter concurrency headroom and pending > live. live=None is the historical path."""

    def test_busy_project_default_concurrency_stays_serial(self):
        root = _ws([("a", "qa_review"), ("b", "qa_review")])
        self.assertIsNone(router.decide(root, "p", live={"qa": 1}))

    def test_stacks_live_role_with_headroom_and_extra_pending(self):
        root = _ws([("a", "qa_review"), ("b", "qa_review")])
        _with_frontmatter(root, "qa", ["concurrency: 2"])
        self.assertEqual(router.decide(root, "p", live={"qa": 1}), "qa")

    def test_no_stack_at_declared_capacity(self):
        root = _ws([("a", "qa_review"), ("b", "qa_review"), ("c", "qa_review")])
        _with_frontmatter(root, "qa", ["concurrency: 2"])
        self.assertIsNone(router.decide(root, "p", live={"qa": 2}))

    def test_no_stack_without_extra_pending_task(self):
        # one qa_review task, one live qa run: a second launch would duplicate the same task
        root = _ws([("a", "qa_review")])
        _with_frontmatter(root, "qa", ["concurrency: 3"])
        self.assertIsNone(router.decide(root, "p", live={"qa": 1}))

    def test_never_launches_a_second_role_into_a_busy_repo(self):
        # engineer live; top pending work wants qa — cross-role stacking stays off
        root = _ws([("a", "qa_review"), ("b", "ready")])
        _with_frontmatter(root, "qa", ["concurrency: 2"])
        self.assertIsNone(router.decide(root, "p", live={"engineer": 1}))

    def test_agent_setup_sanitizes_concurrency(self):
        root = _ws([])
        _with_frontmatter(root, "qa", ["concurrency: 3"])
        _with_frontmatter(root, "engineer", ["concurrency: nine"])
        self.assertEqual(router.agent_setup(root, "p", "qa")["concurrency"], "3")
        self.assertEqual(router.agent_setup(root, "p", "engineer")["concurrency"], "1")
        self.assertEqual(router.agent_setup(root, "p", "lead")["concurrency"], "1")  # unset


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

    # --- the harness-side idle check (plan 1.5): a cadence role whose interval elapsed is still
    # skipped when the board is exactly as it left it, until a 24h heartbeat ---------------
    def _lead_ws(self, tasks=(("a", "approved"),), ran_hours_ago=6):
        root = _ws(list(tasks), roles=ROLES_WITH_LEAD)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        conn = sqlite3.connect(os.path.join(root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,status,started_at) VALUES('p','lead','succeeded',"
                     "datetime('now','-%d hours'))" % ran_hours_ago)
        conn.commit(); conn.close()
        return root

    def _mark(self, root, fp, hours_old=0):
        import time
        p = os.path.join(root, "projects", "p", ".cadence-lead")
        with open(p, "w") as f:
            f.write(fp + "\n")
        if hours_old:
            t = time.time() - hours_old * 3600
            os.utime(p, (t, t))
        return p

    def test_board_fingerprint_tracks_status_priority_and_membership_not_notes(self):
        root = self._lead_ws()
        conn = sqlite3.connect(os.path.join(root, "dais.db"))
        conn.execute("ALTER TABLE tasks ADD COLUMN notes TEXT")
        f0 = router.board_fingerprint(conn, "p")
        conn.execute("UPDATE tasks SET notes='a QA note' WHERE id='a'")
        self.assertEqual(router.board_fingerprint(conn, "p"), f0)          # notes don't count
        conn.execute("UPDATE tasks SET priority='high' WHERE id='a'")
        f1 = router.board_fingerprint(conn, "p"); self.assertNotEqual(f1, f0)
        conn.execute("UPDATE tasks SET status='done' WHERE id='a'")
        f2 = router.board_fingerprint(conn, "p"); self.assertNotEqual(f2, f1)
        conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('b','p','b','proposed')")
        self.assertNotEqual(router.board_fingerprint(conn, "p"), f2)

    def test_cadence_skipped_while_the_board_is_as_the_role_left_it(self):
        root = self._lead_ws()
        conn = sqlite3.connect(os.path.join(root, "dais.db"))
        self._mark(root, router.board_fingerprint(conn, "p"))
        self.assertIsNone(router.decide(root, "p"))

    def test_cadence_runs_again_when_the_board_changed(self):
        root = self._lead_ws()
        conn = sqlite3.connect(os.path.join(root, "dais.db"))
        self._mark(root, router.board_fingerprint(conn, "p"))
        conn.execute("UPDATE tasks SET priority='high' WHERE id='a'"); conn.commit()
        self.assertEqual(router.decide(root, "p"), "lead")

    def test_cadence_heartbeat_runs_a_stale_marker(self):
        root = self._lead_ws(ran_hours_ago=30)
        conn = sqlite3.connect(os.path.join(root, "dais.db"))
        self._mark(root, router.board_fingerprint(conn, "p"), hours_old=30)
        self.assertEqual(router.decide(root, "p"), "lead")

    def test_quiet_hours_window_math(self):
        # plan 3.6: "23-7" spans midnight; "9-17" does not; '' = no window
        self.assertTrue(router.in_quiet_hours("23-7", 2))
        self.assertTrue(router.in_quiet_hours("23-7", 23))
        self.assertFalse(router.in_quiet_hours("23-7", 7))
        self.assertFalse(router.in_quiet_hours("23-7", 12))
        self.assertTrue(router.in_quiet_hours("9-17", 12))
        self.assertFalse(router.in_quiet_hours("9-17", 8))
        self.assertFalse(router.in_quiet_hours("", 3))
        self.assertFalse(router.in_quiet_hours("night", 3))   # unparseable = no window

    def test_cadence_respects_quiet_hours_but_reactive_work_does_not(self):
        root = self._lead_ws()                               # lead's 5h cadence has elapsed
        with open(os.path.join(root, "projects", "p", "project.yaml"), "w") as f:
            f.write("project: p\nrepo: p\nstage_goal: x\nquiet_hours: 23-7\n")
        old = os.environ.get("DAIS_NOW_HOUR"); os.environ["DAIS_NOW_HOUR"] = "3"
        self.addCleanup(lambda: os.environ.__setitem__("DAIS_NOW_HOUR", old) if old else os.environ.pop("DAIS_NOW_HOUR", None))
        self.assertIsNone(router.decide(root, "p"))          # 03:00 -> the lead sleeps
        os.environ["DAIS_NOW_HOUR"] = "10"
        self.assertEqual(router.decide(root, "p"), "lead")   # 10:00 -> runs
        os.environ["DAIS_NOW_HOUR"] = "3"
        conn = sqlite3.connect(os.path.join(root, "dais.db"))
        conn.execute("INSERT INTO tasks(id,project,title,status) VALUES('r','p','urgent','ready')"); conn.commit()
        self.assertEqual(router.decide(root, "p"), "engineer")   # reactive work ignores quiet hours

    def test_cadence_without_a_marker_runs_as_before(self):
        self.assertEqual(router.decide(self._lead_ws(), "p"), "lead")

    def test_blocked_work_falls_through_to_cadence_not_reactive(self):
        # a proposed task blocked on an open predecessor is NOT reactive; with no cadence lead it idles
        # (proving the blocked task itself didn't trigger a dispatch).
        self.assertIsNone(router.decide(_ws([("a", "proposed", "b"), ("b", "approved")]), "p"))


class TestSpendCeiling(unittest.TestCase):
    """Task spend ceiling (plan 1.6): project.yaml task_max_runs / task_max_tokens. A task's
    spend = the DISTINCT runs that touched it (run_tasks) and their prompt tokens; over either
    ceiling the dispatcher skips the task like a dep-blocked one, until the founder lifts it
    (tasks.budget_lifted_at: only runs after the stamp count)."""

    def setUp(self):
        self.root = _ws([("r-1", "ready"), ("r-2", "ready", None, "low")])
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.executescript("ALTER TABLE runs ADD COLUMN input_tokens INTEGER;"
                           "ALTER TABLE tasks ADD COLUMN budget_lifted_at TEXT;"
                           "CREATE TABLE run_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,"
                           " task_id TEXT, verb TEXT, at TEXT);")
        for i in range(1, 5):                       # four runs on r-1, 100k each
            conn.execute("INSERT INTO runs(id,project,agent,status,started_at,input_tokens) "
                         "VALUES(?,'p','engineer','succeeded',datetime('now','-%d hours'),100000)" % (10 - i), (i,))
            conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(?,'r-1','claim')", (i,))
            conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(?,'r-1','touch')", (i,))  # same run, twice
        conn.commit(); conn.close()

    def _yaml(self, text):
        with open(os.path.join(self.root, "projects", "p", "project.yaml"), "w") as f:
            f.write("project: p\nrepo: p\nstage_goal: x\n" + text)

    def test_no_ceiling_means_nothing_is_over_budget(self):
        self._yaml("")
        self.assertEqual(router.over_budget_tasks(self.root, "p"), {})

    def test_run_ceiling_counts_distinct_runs(self):
        self._yaml("task_max_runs: 4\n")           # 4 runs = at the ceiling -> over
        over = router.over_budget_tasks(self.root, "p")
        self.assertEqual(set(over), {"r-1"})
        self.assertEqual(over["r-1"]["runs"], 4)
        self._yaml("task_max_runs: 5\n")
        self.assertEqual(router.over_budget_tasks(self.root, "p"), {})

    def test_token_ceiling_sums_prompt_tokens(self):
        self._yaml("task_max_tokens: 350k\n")
        over = router.over_budget_tasks(self.root, "p")
        self.assertEqual(over["r-1"]["tokens"], 400000)
        self._yaml("task_max_tokens: 1M\n")
        self.assertEqual(router.over_budget_tasks(self.root, "p"), {})

    def test_a_lift_counts_only_runs_after_the_stamp(self):
        self._yaml("task_max_runs: 3\n")
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("UPDATE tasks SET budget_lifted_at=datetime('now','-7 hours') WHERE id='r-1'")
        conn.commit(); conn.close()                 # runs 1,2 (9h, 8h ago) are before the stamp
        self.assertEqual(router.over_budget_tasks(self.root, "p"), {})

    def test_dispatch_skips_an_over_budget_task_and_takes_the_next(self):
        self._yaml("task_max_runs: 2\n")
        self.assertEqual(router.dispatch_next(self.root, "p"), ("engineer", "r-2"))
        self._yaml("task_max_runs: 20\n")
        self.assertEqual(router.dispatch_next(self.root, "p"), ("engineer", "r-1"))

    def test_budget_strings(self):
        self.assertEqual(router.parse_budget("2M"), ("tokens", 2000000))
        self.assertEqual(router.parse_budget("350k"), ("tokens", 350000))
        self.assertEqual(router.parse_budget("1500"), ("tokens", 1500))
        self.assertEqual(router.parse_budget("$20"), ("usd", 20.0))
        self.assertEqual(router.parse_budget("$2.50"), ("usd", 2.5))
        self.assertIsNone(router.parse_budget("lots"))
        self.assertIsNone(router.parse_budget(""))


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


class TestAgentSetup(unittest.TestCase):
    """One resolution authority: frontmatter -> legacy roles file -> project.yaml -> defaults;
    access: machine.json roles -> legacy roles file -> review."""
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dais-as-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pdir = os.path.join(self.root, "projects", "demo")
        os.makedirs(os.path.join(self.pdir, "agents"))
        with open(os.path.join(self.pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: demo\nmodel: claude-opus-4-8\neffort: high\n"
                    "model_qa: claude-haiku-4-5\nstage_goal: x\n")
        # a machine whose roles carry access (the new authority)
        with open(os.path.join(self.pdir, "machine.json"), "w") as f:
            f.write('{"name":"t","entry":"ready","roles":{"engineer":{"access":"edit"},'
                    '"qa":{"access":"review"}},'
                    '"states":{"ready":{"initial":true},"done":{"terminal":true}},'
                    '"edges":[{"from":"ready","to":"done","by":"engineer","verb":"finish"}]}')

    def _agent(self, role, fm=""):
        with open(os.path.join(self.pdir, "agents", role + ".md"), "w") as f:
            f.write((("---\n%s---\n" % fm) if fm else "") + "You are %s.\n" % role)

    def test_frontmatter_wins_over_suffix_key(self):
        self._agent("qa", "model: claude-sonnet-5\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual(s["model"], "claude-sonnet-5")

    def test_suffix_key_wins_over_project_default(self):
        self._agent("qa")
        self.assertEqual(router.agent_setup(self.root, "demo", "qa")["model"], "claude-haiku-4-5")

    def test_project_default_then_tool_default(self):
        self._agent("engineer")
        s = router.agent_setup(self.root, "demo", "engineer")
        self.assertEqual(s["model"], "claude-opus-4-8")     # project-wide
        self.assertEqual(s["effort"], "high")

    def test_access_from_machine_roles(self):
        self._agent("engineer")
        self.assertEqual(router.agent_setup(self.root, "demo", "engineer")["access"], "edit")

    def test_access_legacy_roles_file_then_review_default(self):
        self._agent("lead")
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("lead  draft  every:5h  -  3  plan\n")
        s = router.agent_setup(self.root, "demo", "lead")
        self.assertEqual(s["access"], "draft")              # legacy roles file (not in machine)
        self.assertEqual(s["trigger"], "every:5h")
        self.assertEqual(s["prec"], "3")
        self.assertEqual(s["playbook"], "plan")
        os.remove(os.path.join(self.pdir, "roles"))
        s = router.agent_setup(self.root, "demo", "lead")
        self.assertEqual(s["access"], "review")             # safe default
        self.assertEqual(s["trigger"], "reactive")
        self.assertEqual(s["prec"], "50")

    # --- the lean agent profile (plan 1.3): context / mcp / plugins ---------------------------
    def test_context_defaults_to_lean_and_resolves_like_model(self):
        self._agent("qa")
        self.assertEqual(router.agent_setup(self.root, "demo", "qa")["context"], "lean")
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("context: full\n")
        self.assertEqual(router.agent_setup(self.root, "demo", "qa")["context"], "full")
        self._agent("qa", "context: lean\n")                    # frontmatter wins
        self.assertEqual(router.agent_setup(self.root, "demo", "qa")["context"], "lean")
        self._agent("qa", "context: fulll\n")                   # a typo must not widen the profile
        self.assertEqual(router.agent_setup(self.root, "demo", "qa")["context"], "lean")

    def test_mcp_and_plugins_allowlists_are_comma_lists(self):
        self._agent("qa", "mcp: qmd, gbrain\nplugins: supabase\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual(s["mcp"], "qmd,gbrain")
        self.assertEqual(s["plugins"], "supabase")
        self._agent("engineer")
        s = router.agent_setup(self.root, "demo", "engineer")
        self.assertEqual((s["mcp"], s["plugins"]), ("", ""))
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("plugins: supabase, superpowers\n")          # project-wide default
        self.assertEqual(router.agent_setup(self.root, "demo", "engineer")["plugins"], "supabase,superpowers")

    # --- budget caps (plan 1.4): max_turns / max_budget_usd / max_minutes --------------------
    def test_caps_default_to_unbounded_and_resolve_like_model(self):
        self._agent("qa")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual((s["max_turns"], s["max_budget_usd"], s["max_minutes"]), ("", "", ""))
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("max_turns: 40\nmax_minutes: 30\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual((s["max_turns"], s["max_minutes"]), ("40", "30"))
        self._agent("qa", "max_turns: 25\nmax_budget_usd: 2.50\nmax_minutes: 0.5\n")   # frontmatter wins
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual((s["max_turns"], s["max_budget_usd"], s["max_minutes"]), ("25", "2.50", "0.5"))

    def test_caps_that_do_not_parse_are_unbounded_not_surprising(self):
        self._agent("qa", "max_turns: many\nmax_budget_usd: -1\nmax_minutes: 0\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual((s["max_turns"], s["max_budget_usd"], s["max_minutes"]), ("", "", ""))

    def test_provider_auth_defaults_and_frontmatter(self):
        self._agent("qa", "provider: openai\nauth: api\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual((s["provider"], s["auth"]), ("openai", "api"))
        self._agent("engineer")
        s = router.agent_setup(self.root, "demo", "engineer")
        self.assertEqual((s["provider"], s["auth"]), ("anthropic", "subscription"))

    def test_project_model_does_not_leak_across_providers(self):
        home = tempfile.mkdtemp(prefix="dais-nohome-")     # no ~/.codex/config.toml (plan 1.8 reads it)
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        old = os.environ.get("HOME"); os.environ["HOME"] = home
        self.addCleanup(os.environ.__setitem__, "HOME", old)
        self._agent("qa", "provider: openai\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual(s["provider"], "openai")
        self.assertEqual(s["model"], "")               # codex CLI default, not the anthropic id

    def test_frontmatter_model_applies_to_its_own_provider(self):
        self._agent("qa", "provider: openai\nmodel: gpt-5.2-codex\n")
        self.assertEqual(router.agent_setup(self.root, "demo", "qa")["model"], "gpt-5.2-codex")

    def test_playbook_file_resolved(self):
        os.makedirs(os.path.join(self.pdir, "playbooks"))
        with open(os.path.join(self.pdir, "playbooks", "design.md"), "w") as f:
            f.write("design conventions\n")
        self._agent("qa", "playbook: design\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertTrue(s["playbook_file"].endswith("projects/demo/playbooks/design.md"))

    def test_cli_mode_prints_key_value_lines(self):
        self._agent("qa", "model: claude-sonnet-5\n")
        import subprocess
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(router.__file__), "router.py"),
                              "--agent-config", self.root, "demo", "qa"],
                             capture_output=True, text=True).stdout
        self.assertIn("model=claude-sonnet-5", out)
        self.assertIn("provider=anthropic", out)
        self.assertIn("access=review", out)


class TestCastFromAgents(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dais-cast-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pdir = os.path.join(self.root, "projects", "demo")
        os.makedirs(os.path.join(self.pdir, "agents"))
        with open(os.path.join(self.pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: demo\nstage_goal: x\n")

    def _agent(self, role, fm=""):
        with open(os.path.join(self.pdir, "agents", role + ".md"), "w") as f:
            f.write((("---\n%s---\n" % fm) if fm else "") + "persona\n")

    def test_cast_from_agent_files_with_frontmatter(self):
        self._agent("engineer")
        self._agent("lead", "trigger: every:5h\nprec: 3\n")
        c = {r["name"]: r for r in router.cast(self.root, "demo")}
        self.assertEqual(c["engineer"]["trigger"], "reactive")
        self.assertEqual(c["engineer"]["prec"], 50)
        self.assertEqual(c["lead"]["trigger"], "every:5h")
        self.assertEqual(c["lead"]["prec"], 3)

    def test_legacy_roles_file_still_contributes(self):
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("qa  review  reactive  -  1\n")
        names = {r["name"] for r in router.cast(self.root, "demo")}
        self.assertIn("qa", names)

    def test_frontmatter_beats_legacy_row(self):
        self._agent("lead", "trigger: none\n")
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("lead  review  every:5h  -  3\n")
        c = {r["name"]: r for r in router.cast(self.root, "demo")}
        self.assertEqual(c["lead"]["trigger"], "none")

    def test_empty_project_is_empty_cast(self):
        self.assertEqual(router.cast(self.root, "demo"), [])


class TestLeanProfileHelpers(unittest.TestCase):
    """router.mcp_config_json / plugin_dirs: the lean profile's allowlists resolved against the
    founder's OWN Claude Code install (~/.claude.json user mcpServers; ~/.claude/plugins/cache).
    HOME is pointed at a fixture so the tests never read the real config."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="dais-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(os.environ.__setitem__, "HOME", old)
        import json
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"qmd": {"command": "qmd", "args": ["mcp"]},
                                      "gbrain": {"command": "gbrain", "args": ["mcp"]}}}, f)
        for plug, vers in (("supabase", ["1.0.0", "1.2.0"]), ("superpowers", ["6.2.0"])):
            for v in vers:
                os.makedirs(os.path.join(self.home, ".claude", "plugins", "cache", "official", plug, v))

    def test_openai_default_model_is_the_codex_configs_model(self):
        # plan 1.8: a codex role with no model: ran on the codex CLI's own default and recorded
        # '' on the run row; `dais project` showed "(codex default)". Read ~/.codex/config.toml
        # so the real id is resolved, passed explicitly, and recorded.
        os.makedirs(os.path.join(self.home, ".codex"))
        with open(os.path.join(self.home, ".codex", "config.toml"), "w") as f:
            f.write('model = "gpt-5.6-terra"\nmodel_reasoning_effort = "medium"\n')
        root = tempfile.mkdtemp(prefix="dais-cdx-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pdir = os.path.join(root, "projects", "demo"); os.makedirs(os.path.join(pdir, "agents"))
        with open(os.path.join(pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: x\nstage_goal: g\nmodel: claude-opus-5\n")
        with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
            f.write("---\nprovider: openai\n---\npersona\n")
        self.assertEqual(router.agent_setup(root, "demo", "qa")["model"], "gpt-5.6-terra")
        with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
            f.write("---\nprovider: openai\nmodel: gpt-5.4\n---\npersona\n")   # explicit still wins
        self.assertEqual(router.agent_setup(root, "demo", "qa")["model"], "gpt-5.4")
        os.remove(os.path.join(self.home, ".codex", "config.toml"))          # unreadable -> ''
        with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
            f.write("---\nprovider: openai\n---\npersona\n")
        self.assertEqual(router.agent_setup(root, "demo", "qa")["model"], "")

    def test_mcp_config_holds_only_the_allowlisted_servers(self):
        import json
        cfg = json.loads(router.mcp_config_json("qmd"))
        self.assertEqual(list(cfg["mcpServers"]), ["qmd"])
        self.assertEqual(cfg["mcpServers"]["qmd"]["command"], "qmd")
        self.assertEqual(json.loads(router.mcp_config_json(""))["mcpServers"], {})

    def test_unknown_mcp_name_is_reported_not_silently_dropped(self):
        import json
        cfg, missing = router.mcp_config_json("qmd,nope", report=True)
        self.assertEqual(missing, ["nope"])
        self.assertEqual(list(json.loads(cfg)["mcpServers"]), ["qmd"])

    def test_plugin_dirs_pick_the_newest_cached_version(self):
        dirs, missing = router.plugin_dirs("supabase,superpowers,ghost")
        self.assertEqual([os.path.basename(os.path.dirname(d)) for d in dirs], ["supabase", "superpowers"])
        self.assertTrue(dirs[0].endswith(os.path.join("supabase", "1.2.0")))
        self.assertEqual(missing, ["ghost"])


class TestLintTransitionWarnings(unittest.TestCase):
    """Transition-period lint: legacy-location warnings (roles file, suffix keys,
    active_agents), orphan cast members (agents/<x>.md with no machine role), and
    secret-shaped values in config/persona files. lint_project() must not early-return
    just because the roles file is absent — it lints the new (frontmatter) world too."""
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dais-lintw-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pdir = os.path.join(self.root, "projects", "demo")
        os.makedirs(os.path.join(self.pdir, "agents"))
        with open(os.path.join(self.pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: demo\nstage_goal: x\n")
        with open(os.path.join(self.pdir, "machine.json"), "w") as f:
            f.write('{"name":"t","entry":"ready","roles":{"engineer":{"access":"edit"},'
                    '"qa":{"access":"review"}},'
                    '"states":{"ready":{"initial":true},"done":{"terminal":true}},'
                    '"edges":[{"from":"ready","to":"done","by":"engineer","verb":"finish"}]}')

    def _agent(self, role, fm=""):
        with open(os.path.join(self.pdir, "agents", role + ".md"), "w") as f:
            f.write((("---\n%s---\n" % fm) if fm else "") + "You are %s.\n" % role)

    def test_warns_on_legacy_roles_file(self):
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("qa review reactive - 1\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("legacy roles file" in w for w in warns))

    def _with_path(self, path):
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = path
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def test_warns_when_a_roles_provider_cli_is_not_installed(self):
        # a role on provider openai needs `codex` on PATH; without it the run dies at
        # preflight every tick, so say so at lint time (the founder's first stop)
        self._agent("qa", "provider: openai\n")
        self._with_path("/nonexistent-bin")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("codex" in w and "qa" in w for w in warns), warns)

    def test_warns_on_a_full_context_anthropic_role(self):
        # context: full hands the run the founder's whole Claude Code config — every plugin,
        # every MCP server (mail, payments…), the personal CLAUDE.md. Say so, per role.
        self._agent("engineer", "context: full\n")
        self._agent("qa")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("context: full" in w and "engineer" in w for w in warns), warns)
        self.assertFalse(any("context: full" in w and "'qa'" in w for w in warns), warns)

    def test_warns_when_a_codex_role_sets_a_cap_codex_cannot_enforce(self):
        # codex exec has no --max-turns / budget flag; only max_minutes (the harness's own
        # watchdog) binds a codex run. Say so rather than let the founder believe it's capped.
        self._agent("qa", "provider: openai\nmax_turns: 20\nmax_budget_usd: 1\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("max_turns" in w and "openai" in w for w in warns), warns)
        self._agent("qa", "provider: openai\nmax_minutes: 20\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertFalse(any("max_minutes" in w for w in warns), warns)

    def test_no_provider_cli_warning_when_installed(self):
        b = tempfile.mkdtemp(prefix="dais-bin-")
        self.addCleanup(shutil.rmtree, b, ignore_errors=True)
        for cli in ("codex", "claude"):
            with open(os.path.join(b, cli), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(b, cli), 0o755)
        self._agent("qa", "provider: openai\n")
        self._agent("engineer")
        self._with_path(b)
        _, warns = router.lint_project(self.root, "demo")
        self.assertFalse(any("not on PATH" in w for w in warns), warns)

    def test_warns_on_legacy_suffix_keys_and_active_agents(self):
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("model_qa: claude-haiku-4-5\nactive_agents: qa engineer\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("model_qa" in w for w in warns))
        self.assertTrue(any("active_agents" in w for w in warns))

    def test_warns_on_legacy_suffix_key_with_digit_in_role_slug(self):
        # role slugs may contain digits/dots/dashes (e.g. a second qa instance "qa2") — the
        # suffix-key warning regex must match the full role-slug charset, not just [a-z_]+.
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("model_qa2: claude-haiku-4-5\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("model_qa2" in w for w in warns))

    def test_warns_on_agent_file_with_no_machine_role(self):
        self._agent("ghost")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("ghost" in w and "machine" in w for w in warns))

    def test_warns_on_oversized_context(self):
        # a bloated CONTEXT.md is injected into EVERY agent run and silently truncated by
        # the Read cap — the log miners found 25K tokens/run burned on a file agents could
        # only half-read. Lint catches it before it taxes every run.
        with open(os.path.join(self.pdir, "CONTEXT.md"), "w") as f:
            f.write("x" * 40000)
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("CONTEXT.md" in w and "every agent run" in w for w in warns))

    def test_warns_on_secret_shaped_value(self):
        self._agent("qa", "model: claude-opus-4-8\n")
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("api_key: sk-ant-abc123def456ghi789\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("secret" in w.lower() for w in warns))

    def test_no_roles_file_is_not_an_early_return(self):
        # trigger/access sanity now runs off the cast, not the roles file
        self._agent("qa", "trigger: sometimes\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("trigger" in w for w in warns))

    def test_warns_on_schedulable_cast_member_without_persona(self):
        # a stale roles-file row (retired role): schedulable, machine doesn't dispatch it,
        # no agents/<role>.md — decide() could still pick it and stall silently
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("ghostlead  review  every:5h  -  3\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("ghostlead" in w and "persona" in w for w in warns))


class TestLintEnumeratesWithoutRolesFile(unittest.TestCase):
    """lint() enumerates projects by project.yaml presence — the roles file is legacy and
    optional, so a project.yaml-only project (no roles file) must still be linted, not skipped."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dais-lint-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pdir = os.path.join(self.root, "projects", "demo")
        os.makedirs(os.path.join(self.pdir, "agents"))
        with open(os.path.join(self.pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: demo\nstage_goal: x\n")
        with open(os.path.join(self.pdir, "machine.json"), "w") as f:
            f.write('{"name":"t","entry":"ready","roles":{"engineer":{"access":"edit"}},'
                    '"states":{"ready":{"initial":true},"done":{"terminal":true}},'
                    '"edges":[{"from":"ready","to":"done","by":"engineer","verb":"finish"}]}')
        # deliberately no roles file

    def test_lint_enumerates_project_without_roles_file(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            router.lint(self.root, "")
        self.assertIn("demo", buf.getvalue())


class TestIsolationConfig(unittest.TestCase):
    """agent_setup resolves `isolation` (per-run worktree opt-in): frontmatter -> project.yaml ->
    default 'none'. run-agent.sh reads it to decide whether to give the run a private worktree."""

    def _proj_yaml(self, root, text):
        with open(os.path.join(root, "projects", "p", "project.yaml"), "w") as f:
            f.write(text)

    def test_defaults_to_none(self):
        self.assertEqual(router.agent_setup(_ws([("a", "ready")]), "p", "engineer")["isolation"], "none")

    def test_frontmatter_opt_in(self):
        root = _ws([("a", "ready")])
        _with_frontmatter(root, "engineer", ["isolation: worktree"])
        self.assertEqual(router.agent_setup(root, "p", "engineer")["isolation"], "worktree")

    def test_project_yaml_default(self):
        root = _ws([("a", "ready")])
        self._proj_yaml(root, "isolation: worktree\n")
        self.assertEqual(router.agent_setup(root, "p", "engineer")["isolation"], "worktree")

    def test_frontmatter_overrides_project_yaml(self):
        root = _ws([("a", "ready")])
        self._proj_yaml(root, "isolation: worktree\n")
        _with_frontmatter(root, "engineer", ["isolation: none"])
        self.assertEqual(router.agent_setup(root, "p", "engineer")["isolation"], "none")


class TestDispatchNext(unittest.TestCase):
    """dispatch_next — the reactive (role, task) the dispatcher would launch, so run-agent can pin
    the triggering task to the run (DAIS_TASK_ID). Mirrors decide()'s reactive path."""

    def test_returns_role_and_triggering_task(self):
        self.assertEqual(router.dispatch_next(_ws([("a", "ready")]), "p"), ("engineer", "a"))

    def test_idle_returns_empty_pair(self):
        # 'approved' is release-parked: no dispatch role, so nothing to pin
        self.assertEqual(router.dispatch_next(_ws([("a", "approved")]), "p"), ("", ""))

    def test_the_trigger_is_the_top_priority_task(self):
        root = _ws([("a", "ready", None, "medium"), ("b", "ready", None, "high")])
        self.assertEqual(router.dispatch_next(root, "p"), ("engineer", "b"))

    def test_role_matches_what_decide_reactively_picks(self):
        root = _ws([("a", "qa_review")])
        role, task = router.dispatch_next(root, "p")
        self.assertEqual(role, router.decide(root, "p"))
        self.assertEqual((role, task), ("qa", "a"))

    def test_task_held_by_a_live_run_is_not_handed_out_twice(self):
        root = _ws([("a", "qa_review"), ("b", "qa_review")])
        self.assertEqual(router.dispatch_next(root, "p"), ("qa", "a"))
        _pin(root, "qa", "a")                                   # run 1 pins the top task
        self.assertEqual(router.dispatch_next(root, "p"), ("qa", "b"))

    def test_a_finished_run_releases_its_task(self):
        root = _ws([("a", "qa_review")])
        _pin(root, "qa", "a", status="interrupted")
        self.assertEqual(router.dispatch_next(root, "p"), ("qa", "a"))

    def test_stacked_run_takes_the_second_task(self):
        # the whole point, end to end (issue #7): qa live on 'a' with concurrency 2 → decide stacks a
        # second qa run, and that run must pin 'b'. Before the fix both runs read "dispatched for a".
        root = _ws([("a", "qa_review"), ("b", "qa_review")])
        _with_frontmatter(root, "qa", ["concurrency: 2"])
        _pin(root, "qa", "a")
        self.assertEqual(router.decide(root, "p", live={"qa": 1}), "qa")
        self.assertEqual(router.dispatch_next(root, "p"), ("qa", "b"))


if __name__ == "__main__":
    unittest.main()


class TestProviderPacks(unittest.TestCase):
    """Provider packs (plan 5.1): harness/providers/<name>/{pack.json, run.sh, stream.py, caps.txt}.
    Everything provider-specific is discovered from the directory — no names in code."""

    def test_stock_packs_are_discovered_with_their_meta(self):
        packs = router.provider_packs()
        self.assertEqual(sorted(packs), ["anthropic", "openai"])
        self.assertEqual(packs["anthropic"]["cli"], "claude")
        self.assertEqual(packs["openai"]["cli"], "codex")
        self.assertEqual(packs["anthropic"]["key_var"], "ANTHROPIC_API_KEY")
        self.assertEqual(packs["openai"]["key_var"], "OPENAI_API_KEY")
        for name in packs:
            d = router.pack_dir(name)
            for f in ("pack.json", "run.sh", "stream.py", "caps.txt"):
                self.assertTrue(os.path.exists(os.path.join(d, f)), (name, f))

    def test_provider_cli_map_derives_from_the_packs(self):
        self.assertEqual(router.PROVIDER_CLI, {n: p["cli"] for n, p in router.provider_packs().items()})

    def test_default_model_comes_from_the_pack(self):
        self.assertEqual(router.provider_packs()["anthropic"].get("default_model"), "claude-opus-4-8")
