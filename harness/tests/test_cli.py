import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# lib.sh's sqlite3 preflight (lib.sh:6) fires at SOURCE time, so a too-narrow PATH
# would make sourcing exit 127 wherever sqlite3 lives outside /usr/bin (e.g. Homebrew's
# /opt/homebrew/bin). Include sqlite3's real dir so the preflight passes everywhere.
_SQLITE_DIR = os.path.dirname(shutil.which("sqlite3") or "/usr/bin/sqlite3")
_PREFLIGHT_PATH = "%s:/usr/bin:/bin" % _SQLITE_DIR


def make_sandbox():
    """A throwaway copy of the harness so CLI tests never touch the live dais.db.
    DAIS_ROOT resolves to this dir (dais computes it from its own location)."""
    root = tempfile.mkdtemp(prefix="dais-cli-")
    shutil.copytree(os.path.join(REPO, "harness"), os.path.join(root, "harness"))
    shutil.copy2(os.path.join(REPO, "dais"), os.path.join(root, "dais"))
    os.chmod(os.path.join(root, "dais"), 0o755)
    os.mkdir(os.path.join(root, "projects"))
    return root


def dais(root, *args, env=None):
    e = dict(os.environ)
    e["NO_COLOR"] = "1"
    if env:
        e.update(env)
    return subprocess.run([os.path.join(root, "dais"), *args],
                          capture_output=True, text=True, env=e, cwd=root)


def q(root, sql):
    c = sqlite3.connect(os.path.join(root, "dais.db"))
    try:
        return c.execute(sql).fetchone()
    finally:
        c.close()


# A pristine workspace, built ONCE. `dais init` is ~130ms of subprocess (bash + the sqlite
# schema + every migration); running it in every test's setUp dominated this file's runtime
# (~11s across the suite). Snapshot init's output here and clone the few small files per test
# instead — behavior-identical (the post-state is an inited workspace) but ~instant. The
# harness itself is still copied per test (only ~16ms) so the migration tests keep their own
# writable harness/migrations, and DAIS_ROOT stays isolated from the live repo.
def _pristine_workspace():
    sb = make_sandbox()
    try:
        dais(sb, "init")
        out = {}
        for name in ("dais.yaml", "CONTEXT.md", ".gitignore", "dais.db"):
            p = os.path.join(sb, name)
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    out[name] = fh.read()
        return out
    finally:
        shutil.rmtree(sb, ignore_errors=True)


_PRISTINE = _pristine_workspace()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name, data in _PRISTINE.items():        # clone the inited workspace — no per-test `dais init`
            with open(os.path.join(self.root, name), "wb") as fh:
                fh.write(data)


class TestStatusAndTitle(CliTest):
    def test_proposed_is_a_valid_status(self):
        dais(self.root, "task", "add", "demo", "An initiative", "--id", "d-1")
        r = dais(self.root, "task", "set", "d-1", "--status", "proposed")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("invalid status", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-1'")[0], "proposed")

    def test_task_set_title(self):
        dais(self.root, "task", "add", "demo", "Old title", "--id", "d-2")
        r = dais(self.root, "task", "set", "d-2", "--title", "New title")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT title FROM tasks WHERE id='d-2'")[0], "New title")


class TestTaskAddTouchesMigrations(CliTest):
    """`task add --touches-migrations` (same semantics as `task set`): the winterbraid lead files
    release tasks with this flag AT CREATION — it used to be silently swallowed (not a recognized
    flag, so it hit the generic "unknown flag" catch and the task was never even created), leaving
    releases with NULL touches_migrations. Same true/false vocabulary as `task set`."""

    def test_true_sets_the_column(self):
        r = dais(self.root, "task", "add", "demo", "Release", "--id", "d-1",
                 "--touches-migrations", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT touches_migrations FROM tasks WHERE id='d-1'")[0], 1)

    def test_false_sets_the_column(self):
        r = dais(self.root, "task", "add", "demo", "Release", "--id", "d-1",
                 "--touches-migrations", "false")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT touches_migrations FROM tasks WHERE id='d-1'")[0], 0)

    def test_omitted_leaves_it_null(self):
        dais(self.root, "task", "add", "demo", "Release", "--id", "d-1")
        self.assertIsNone(q(self.root, "SELECT touches_migrations FROM tasks WHERE id='d-1'")[0])

    def test_bogus_value_is_rejected(self):
        r = dais(self.root, "task", "add", "demo", "Release", "--id", "d-1",
                 "--touches-migrations", "maybe")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("true|false", r.stdout + r.stderr)
        self.assertIsNone(q(self.root, "SELECT 1 FROM tasks WHERE id='d-1'"))


class TestTaskAddUnknownFlagIsAnError(CliTest):
    """Silent-ignore is the deeper bug: an unrecognized `--flag` must fail loud, both at the bash
    CLI layer AND at machine.py's `create` — the single authority `task add` delegates to — so a
    typo can never silently create a task with the intended field dropped on the floor."""

    def test_bash_layer_rejects_unknown_flag(self):
        r = dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--bogus", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown flag", r.stdout + r.stderr)
        self.assertIsNone(q(self.root, "SELECT 1 FROM tasks WHERE id='d-1'"))

    def test_machine_create_rejects_unknown_flag_directly(self):
        # bypass the bash CLI's own filter — call the authority machine.py delegates to directly,
        # proving IT no longer silently drops an unrecognized argument either.
        import subprocess as sp
        mp = os.path.join(self.root, "harness", "machines", "coding.machine.json")
        r = sp.run(["python3", os.path.join(self.root, "harness", "machine.py"),
                    "create", os.path.join(self.root, "dais.db"), mp, "demo", "x",
                    "--bogus", "1"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown argument", r.stdout + r.stderr)


class TestAgentStateSurgery(CliTest):
    """Agents (runs carrying DAIS_RUN_ID) may NOT set --status — state changes go through edges
    (dais fire), which is how the cou-21 incident happened: an agent raw-set a founder-semantic
    state. Metadata (--notes/--pr/...) stays allowed; the founder's shell (no run id) is unaffected."""

    def test_agent_status_set_is_refused(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1")
        r = dais(self.root, "task", "set", "d-1", "--status", "ready", env={"DAIS_RUN_ID": "7"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fire", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-1'")[0], "proposed")

    def test_agent_notes_still_allowed(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1")
        r = dais(self.root, "task", "set", "d-1", "--notes", "hello", env={"DAIS_RUN_ID": "7"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_notes_append_is_the_log(self):
        # notes are the agents' only channel: --notes APPENDS an attributed, timestamped
        # entry — a handoff can never clobber the spec the next reader still needs.
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--notes", "SPEC: do the thing")
        r = dais(self.root, "task", "set", "d-1", "--notes", "handoff: verify A and B",
                 env={"DAIS_ACTOR": "engineer"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        notes = q(self.root, "SELECT notes FROM tasks WHERE id='d-1'")[0]
        self.assertIn("SPEC: do the thing", notes)           # prior entry preserved
        self.assertIn("handoff: verify A and B", notes)
        self.assertIn("[engineer", notes)                    # attributed to the actor
        dais(self.root, "task", "set", "d-1", "--notes", "checked")
        notes = q(self.root, "SELECT notes FROM tasks WHERE id='d-1'")[0]
        self.assertIn("[founder", notes)                     # a plain shell is the founder

    def test_replace_notes_is_founder_surgery(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--notes", "old spec")
        dais(self.root, "task", "set", "d-1", "--replace-notes", "clean slate")
        notes = q(self.root, "SELECT notes FROM tasks WHERE id='d-1'")[0]
        self.assertEqual(notes, "clean slate")

    def test_task_show_is_the_full_record(self):
        # one command answers "what is this task" — fields, links, the notes log — so
        # agents stop spelunking dais.db with find/.schema (a third of QA tool calls).
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "The work", "--id", "d-1", "--notes", "SPEC: bar")
        dais(self.root, "task", "set", "d-1", "--pr", "https://x/pull/9",
             "--notes", "handoff: verify A", env={"DAIS_ACTOR": "engineer"})
        r = dais(self.root, "task", "show", "d-1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for needle in ("The work", "proposed", "https://x/pull/9", "SPEC: bar",
                       "handoff: verify A", "[engineer"):
            self.assertIn(needle, r.stdout)

    def test_task_show_unknown_id_fails(self):
        dais(self.root, "scaffold", "demo")
        r = dais(self.root, "task", "show", "nope-1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no task", r.stdout + r.stderr)

    def test_founder_status_set_still_works(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1")
        r = dais(self.root, "task", "set", "d-1", "--status", "ready")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-1'")[0], "ready")


class TestFireLinksRun(CliTest):
    """A successful `dais fire` under an agent run (DAIS_RUN_ID) records a run_tasks row with the
    FIRED VERB — the authoritative trail the migration doc promised ('claim' etc). Without it,
    fire-only runs look like no-ops to the no-progress throttle and get wrongly cooldown'd."""

    def _run_row(self):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,started_at,status) "
                     "VALUES('demo','engineer',datetime('now'),'running')")
        conn.commit()
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.close()
        return rid

    def test_fire_records_the_verb(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--status", "ready")
        rid = self._run_row()
        r = dais(self.root, "fire", "d-1", "claim",
                 env={"DAIS_RUN_ID": str(rid), "DAIS_ACTOR": "engineer"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = q(self.root, "SELECT verb FROM run_tasks WHERE run_id=%d AND task_id='d-1'" % rid)
        self.assertIn("claim", rows)

    def test_failed_fire_records_nothing(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1")   # proposed — claim invalid
        rid = self._run_row()
        r = dais(self.root, "fire", "d-1", "claim",
                 env={"DAIS_RUN_ID": str(rid), "DAIS_ACTOR": "engineer"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(q(self.root, "SELECT COUNT(*) FROM run_tasks WHERE run_id=%d" % rid), (0,))


class TestDepBlockedFire(CliTest):
    """An agent can't fire an edge on a dep-blocked task (blocked_on open) — the chain must bind
    the AGENT, not just the dispatcher (win-131/cou-19 were built early through this hole). The
    founder can still act (defer, cancel — surgery stays)."""

    def _two(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "pred", "--id", "b-1")
        dais(self.root, "task", "add", "demo", "work", "--id", "a-1", "--status", "ready")
        dais(self.root, "task", "set", "a-1", "--depends-on", "b-1")

    def test_agent_claim_on_blocked_task_refused(self):
        self._two()
        r = dais(self.root, "fire", "a-1", "claim", env={"DAIS_ACTOR": "engineer"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("block", (r.stdout + r.stderr).lower())
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='a-1'")[0], "ready")

    def test_founder_can_still_act_on_blocked_task(self):
        self._two()
        r = dais(self.root, "fire", "a-1", "defer")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='a-1'")[0], "deferred")

    def test_agent_claim_allowed_once_dependency_done(self):
        self._two()
        dais(self.root, "task", "set", "b-1", "--status", "done")
        r = dais(self.root, "fire", "a-1", "claim", env={"DAIS_ACTOR": "engineer"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='a-1'")[0], "doing")


class TestCostCommand(CliTest):
    """`dais cost` and the ledger columns in `dais logs` — the CLI seams over harness/cost.py."""

    def _seed(self):
        import sqlite3
        dais(self.root, "scaffold", "demo")
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,started_at,ended_at,status,provider,input_tokens,"
                     "output_tokens,cache_read_tokens,cost_usd,turns,log_path) VALUES('demo','qa',datetime('now'),"
                     "datetime('now'),'succeeded','anthropic',12345,678,9000,0.5,7,'/tmp/x.log')")
        conn.commit(); conn.close()

    def test_cost_reports_the_ledger(self):
        self._seed()
        r = dais(self.root, "cost")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 runs", r.stdout)
        self.assertIn("12k in", r.stdout)
        self.assertIn("$0.50", r.stdout)
        r = dais(self.root, "cost", "demo", "--since", "7d", "--by", "role")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("demo/qa", r.stdout)

    def test_cost_rejects_a_bad_flag(self):
        self._seed()
        r = dais(self.root, "cost", "--bye", "role")
        self.assertNotEqual(r.returncode, 0)

    def test_logs_show_tokens_and_cost(self):
        self._seed()
        r = dais(self.root, "logs", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("12345 tok", r.stdout)
        self.assertIn("$0.50", r.stdout)


class TestProviderScopedGates(CliTest):
    """The cap-cooldown and error-backoff gates are scoped to the PROVIDER that tripped them.
    They were workspace-global: one Claude subscription-window cap parked every project for 90
    minutes, including roles on codex whose ChatGPT allotment was untouched (and a codex rate
    limit would park Claude). A cooled provider's roles are SKIPPED like the no-op throttle
    skips a role — the tick still runs whatever else is dispatchable."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "Build it", "--id", "d-1", "--status", "ready")

    def _engineer_on(self, provider):
        with open(os.path.join(self.root, "projects", "demo", "agents", "engineer.md"), "w") as f:
            f.write("---\nprovider: %s\n---\npersona\n" % provider)

    def _seed_run(self, status, provider, mins_ago, agent="lead"):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,started_at,ended_at,status,provider) "
                     "VALUES('demo',?,datetime('now','-%d minutes'),"
                     "datetime('now','-%d minutes'),?,?)" % (mins_ago, mins_ago),
                     (agent, status, provider))
        conn.commit(); conn.close()

    def test_claude_cap_does_not_park_a_codex_role(self):
        self._engineer_on("openai")
        self._seed_run("capped", "anthropic", 5)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run engineer", r.stdout)

    def test_claude_cap_still_parks_claude_roles(self):
        self._engineer_on("anthropic")
        self._seed_run("capped", "anthropic", 5)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run engineer", r.stdout)
        self.assertIn("cooling", r.stdout)
        self.assertIn("anthropic", r.stdout)

    def test_legacy_null_provider_rows_count_as_anthropic(self):
        # every run before migration 0007 was Claude
        self._engineer_on("anthropic")
        self._seed_run("capped", None, 5)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run engineer", r.stdout)

    def test_success_on_the_same_provider_clears_its_cooldown(self):
        self._engineer_on("anthropic")
        self._seed_run("capped", "anthropic", 10)
        self._seed_run("succeeded", "anthropic", 5)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run engineer", r.stdout)

    def test_success_on_another_provider_does_not_clear_it(self):
        # a codex run succeeding says nothing about the Claude window
        self._engineer_on("anthropic")
        self._seed_run("capped", "anthropic", 10)
        self._seed_run("succeeded", "openai", 5)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run engineer", r.stdout)

    def test_error_backoff_is_per_provider(self):
        self._seed_run("failed", "openai", 5)
        self._seed_run("failed", "openai", 3)
        self._engineer_on("anthropic")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run engineer", r.stdout)
        self._engineer_on("openai")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run engineer", r.stdout)
        self.assertIn("backing off", r.stdout)

    def test_real_tick_reports_backed_off_only_when_a_cooldown_skipped_work(self):
        # exit 20 = "backed off" paces `dais watch` to the long interval; it must mean a
        # cooldown actually withheld a launch, not merely that a cap exists somewhere
        self._engineer_on("anthropic")
        self._seed_run("capped", "anthropic", 5)
        r = dais(self.root, "tick", "demo")
        self.assertEqual(r.returncode, 20, r.stdout + r.stderr)


class TestSpendLimits(CliTest):
    """Plan 1.6 through the CLI: the founder's --budget-lift, and the daily loop budget the
    dispatcher honors (workspace dais.yaml `daily_budget:` / DAIS_DAILY_BUDGET / per-project)."""

    def _spend_today(self, project, tokens, agent="qa"):
        # qa, not engineer: a succeeded engineer run with no verbs would trip the no-op
        # throttle and mask what these tests assert about the budget gate
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,started_at,ended_at,status,provider,input_tokens,cost_usd) "
                     "VALUES(?,?,datetime('now'),datetime('now'),'succeeded','anthropic',?,?)",
                     (project, agent, tokens, tokens / 100000.0))
        conn.commit(); conn.close()

    def test_budget_lift_stamps_the_task(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--status", "ready")
        self.assertIsNone(q(self.root, "SELECT budget_lifted_at FROM tasks WHERE id='d-1'")[0])
        r = dais(self.root, "task", "set", "d-1", "--budget-lift")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(q(self.root, "SELECT budget_lifted_at FROM tasks WHERE id='d-1'")[0])
        self.assertIn("budget_lifted_at", dais(self.root, "task", "show", "d-1").stdout)

    def test_workspace_daily_budget_parks_the_loop(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--status", "ready")
        self._spend_today("demo", 150000)
        with open(os.path.join(self.root, "dais.yaml"), "a") as f:
            f.write("daily_budget: 100k\n")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run", r.stdout)
        self.assertIn("budget", r.stdout.lower())
        r = dais(self.root, "tick", "demo")
        self.assertEqual(r.returncode, 20, r.stdout + r.stderr)
        self.assertIn("budget", open(os.path.join(self.root, "projects", ".watch.log")).read())

    def test_env_override_and_dollar_budgets(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--status", "ready")
        self._spend_today("demo", 150000)                    # = $1.50 by the seed's pricing
        with open(os.path.join(self.root, "dais.yaml"), "a") as f:
            f.write("daily_budget: 100k\n")
        r = dais(self.root, "tick", "demo", "--dry-run", env={"DAIS_DAILY_BUDGET": "1M"})
        self.assertIn("WOULD run engineer", r.stdout)         # `dais watch --budget` wins over dais.yaml
        r = dais(self.root, "tick", "demo", "--dry-run", env={"DAIS_DAILY_BUDGET": "$1"})
        self.assertNotIn("WOULD run", r.stdout)
        r = dais(self.root, "tick", "demo", "--dry-run", env={"DAIS_DAILY_BUDGET": "$2"})
        self.assertIn("WOULD run engineer", r.stdout)

    def test_per_project_daily_budget_skips_only_that_project(self):
        dais(self.root, "scaffold", "demo"); dais(self.root, "scaffold", "other")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--status", "ready")
        dais(self.root, "task", "add", "other", "y", "--id", "o-1", "--status", "ready")
        self._spend_today("demo", 150000)
        with open(os.path.join(self.root, "projects", "demo", "project.yaml"), "a") as f:
            f.write("daily_budget: 100k\n")
        r = dais(self.root, "tick", "--dry-run")
        self.assertIn("tick[other]: WOULD run engineer", r.stdout)
        self.assertNotIn("tick[demo]: WOULD run", r.stdout)
        self.assertIn("budget", r.stdout.lower())


class TestDuplicateWarning(CliTest):
    def test_task_add_warns_and_links_a_near_duplicate(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "Fix the login redirect loop on Safari", "--id", "d-1")
        r = dais(self.root, "task", "add", "demo", "fix login redirect loop safari")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)        # warn, never block
        self.assertIn("possible duplicate", r.stdout)
        self.assertIn("d-1", r.stdout)


class TestDoctor(CliTest):
    """`dais doctor` (plan 2.9): the preflight a founder runs before trusting the loop."""

    def _bin(self, *tools):
        import sys
        b = tempfile.mkdtemp(prefix="dais-bin-"); self.addCleanup(shutil.rmtree, b, ignore_errors=True)
        os.symlink(sys.executable, os.path.join(b, "python3"))
        for t in ("sqlite3", "git"):
            os.symlink(shutil.which(t), os.path.join(b, t))
        for t in tools:
            with open(os.path.join(b, t), "w") as f:
                f.write("#!/bin/sh\n[ \"$1\" = login ] && exit 0\necho ok\n")
            os.chmod(os.path.join(b, t), 0o755)
        return "%s:/usr/bin:/bin" % b

    def test_doctor_reports_a_missing_provider_cli_and_exits_nonzero(self):
        dais(self.root, "scaffold", "demo")
        with open(os.path.join(self.root, "projects", "demo", "agents", "qa.md"), "w") as f:
            f.write("---\nprovider: openai\n---\npersona\n")
        r = dais(self.root, "doctor", env={"PATH": self._bin("claude")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("✗", r.stdout); self.assertIn("codex", r.stdout)
        self.assertIn("✓", r.stdout)                                   # sqlite3/python3/claude lines

    def test_doctor_is_green_when_everything_is_in_place(self):
        dais(self.root, "scaffold", "demo")
        repo_base = tempfile.mkdtemp(prefix="dais-repos-"); self.addCleanup(shutil.rmtree, repo_base, ignore_errors=True)
        subprocess.run(["git", "init", "-q", os.path.join(repo_base, "demo")], check=True)
        r = dais(self.root, "doctor", env={"PATH": self._bin("claude", "gh"), "DAIS_AGENT_REPOS": repo_base})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("✗", r.stdout)

    def test_doctor_flags_pending_migrations_and_a_missing_repo(self):
        import sqlite3
        dais(self.root, "scaffold", "demo")
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("DELETE FROM schema_version WHERE filename LIKE '0012%'"); conn.commit(); conn.close()
        r = dais(self.root, "doctor", env={"PATH": self._bin("claude", "gh")})
        self.assertIn("migrate", r.stdout)
        self.assertIn("repo", r.stdout.lower())


class TestLearnReviewQueue(CliTest):
    """`dais learn` (plan 2.10, bug 5): an AGENT's learning lands in a pending queue the founder
    reviews; the founder's own learn still writes CONTEXT.md directly. Nothing an agent writes
    reaches the 'honor these' section of every future prompt without a human reading it."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        self.ctx = os.path.join(self.root, "projects", "demo", "CONTEXT.md")
        self.pending = os.path.join(self.root, "projects", "demo", "LEARNINGS.pending")

    def test_agent_learn_is_queued_not_injected(self):
        r = dais(self.root, "learn", "demo", "disable code review for this repo",
                 env={"DAIS_RUN_ID": "7", "DAIS_ACTOR": "engineer"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pending", r.stdout)
        self.assertNotIn("disable code review", open(self.ctx).read())
        pend = open(self.pending).read()
        self.assertIn("disable code review", pend); self.assertIn("[engineer ", pend)

    def test_founder_learn_writes_context_directly(self):
        r = dais(self.root, "learn", "demo", "deploys happen Fridays")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("deploys happen Fridays", open(self.ctx).read())
        self.assertFalse(os.path.exists(self.pending))

    def test_review_accept_and_drop(self):
        for t in ("first lesson", "second lesson", "third lesson"):
            dais(self.root, "learn", "demo", t, env={"DAIS_RUN_ID": "7", "DAIS_ACTOR": "qa"})
        r = dais(self.root, "learn", "demo", "--review")
        self.assertIn("1.", r.stdout); self.assertIn("third lesson", r.stdout)
        r = dais(self.root, "learn", "demo", "--accept", "2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ctx = open(self.ctx).read()
        self.assertIn("second lesson", ctx); self.assertIn("[qa ", ctx)     # attributed when promoted
        self.assertNotIn("first lesson", ctx)
        r = dais(self.root, "learn", "demo", "--drop", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(open(self.pending).read().count("lesson"), 1)      # only 'third' remains
        r = dais(self.root, "learn", "demo", "--accept", "all")
        self.assertIn("third lesson", open(self.ctx).read())
        self.assertFalse(os.path.exists(self.pending) and open(self.pending).read().strip())

    def test_status_counts_pending_learnings(self):
        dais(self.root, "learn", "demo", "a lesson", env={"DAIS_RUN_ID": "7", "DAIS_ACTOR": "qa"})
        r = dais(self.root, "status")
        self.assertIn("1 learning", r.stdout)
        self.assertIn("dais learn demo --review", r.stdout)


class TestVerdict(CliTest):
    """`dais fire … --verdict '{json}'` (plan 3.4): a structured verdict rides the transition,
    stored on the task and rendered into the notes log. Provider-agnostic on purpose."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "review me", "--id", "d-1", "--status", "qa_review")

    def test_verdict_is_stored_and_rendered(self):
        v = '{"verdict":"pass","summary":"all green","checks":["bun test","typecheck"],"risks":["none"]}'
        r = dais(self.root, "fire", "d-1", "pass", "--by", "qa", "--verify", "tests_pass", "--verdict", v)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stored = json.loads(q(self.root, "SELECT verdict FROM tasks WHERE id='d-1'")[0])
        self.assertEqual(stored["verdict"], "pass")
        self.assertEqual(stored["by"], "qa")
        self.assertIn("verb", stored)
        notes = q(self.root, "SELECT notes FROM tasks WHERE id='d-1'")[0]
        self.assertIn("verdict: pass", notes); self.assertIn("bun test", notes)
        self.assertIn("verdict", dais(self.root, "task", "show", "d-1").stdout)

    def test_malformed_verdict_refuses_the_fire(self):
        r = dais(self.root, "fire", "d-1", "pass", "--by", "qa", "--verify", "tests_pass", "--verdict", "{not json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verdict", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-1'")[0], "qa_review")


class TestDaisCheck(CliTest):
    """`dais check <task> [<check>] [--branch B]` (plan 2.7): runs the machine's declared check in
    a throwaway worktree of the PR branch, records the result on the task, cleans up."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        self.repo_base = tempfile.mkdtemp(prefix="dais-repos-")
        self.addCleanup(shutil.rmtree, self.repo_base, ignore_errors=True)
        repo = os.path.join(self.repo_base, "demo")
        os.makedirs(repo)
        g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                      capture_output=True, text=True, check=True)
        g("init", "-q", "-b", "main")
        open(os.path.join(repo, "README.md"), "w").write("main\n"); g("add", "."); g("commit", "-qm", "init")
        g("checkout", "-qb", "feature")
        open(os.path.join(repo, "README.md"), "w").write("hello from the feature branch\n")
        g("commit", "-qam", "feature"); g("checkout", "-q", "main")
        self.repo = repo
        # declare the check in the project's own machine copy
        mp = os.path.join(self.root, "projects", "demo", "machine.json")
        m = json.load(open(mp)); m.setdefault("checks", {})["tests_pass"] = "grep -q hello README.md"
        json.dump(m, open(mp, "w"))
        dais(self.root, "task", "add", "demo", "review me", "--id", "d-1", "--status", "qa_review")
        dais(self.root, "task", "set", "d-1", "--pr", "https://x/pull/7")
        self.env = {"DAIS_AGENT_REPOS": self.repo_base}

    def test_check_runs_in_a_worktree_of_the_branch_and_records_the_result(self):
        r = dais(self.root, "check", "d-1", "tests_pass", "--branch", "feature", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("✓", r.stdout)
        rec = json.loads(q(self.root, "SELECT check_results FROM tasks WHERE id='d-1'")[0])
        self.assertEqual((rec["tests_pass"]["ok"], rec["tests_pass"]["pr"]), (1, "https://x/pull/7"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".worktrees", "check-d-1")))   # cleaned up
        # the guard now passes with no --verify self-assertion
        r = dais(self.root, "fire", "d-1", "pass", "--by", "qa")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_failing_check_is_recorded_as_such_and_exits_nonzero(self):
        r = dais(self.root, "check", "d-1", "tests_pass", "--branch", "main", env=self.env)   # main has no 'hello'
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("✗", r.stdout + r.stderr)
        rec = json.loads(q(self.root, "SELECT check_results FROM tasks WHERE id='d-1'")[0])
        self.assertEqual(rec["tests_pass"]["ok"], 0)
        r = dais(self.root, "fire", "d-1", "pass", "--by", "qa")
        self.assertNotEqual(r.returncode, 0)

    def test_check_defaults_to_the_states_verify_guard(self):
        r = dais(self.root, "check", "d-1", "--branch", "feature", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tests_pass", r.stdout)

    def test_undeclared_check_is_an_error(self):
        r = dais(self.root, "check", "d-1", "nope", "--branch", "feature", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nope", r.stdout + r.stderr)


class TestStateEnteredAt(CliTest):
    """Plan 2.3: metadata edits must not reset a gate's age."""

    def test_a_note_leaves_state_entered_at_alone_a_fire_moves_it(self):
        import sqlite3
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "x", "--id", "d-1", "--status", "ready")
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("UPDATE tasks SET state_entered_at='2026-01-01 00:00:00', updated_at='2026-01-01 00:00:00' "
                     "WHERE id='d-1'"); conn.commit(); conn.close()
        dais(self.root, "task", "set", "d-1", "--notes", "still waiting on legal")
        row = q(self.root, "SELECT state_entered_at, updated_at FROM tasks WHERE id='d-1'")
        self.assertEqual(row[0], "2026-01-01 00:00:00")      # the gate is as old as it was
        self.assertNotEqual(row[1], "2026-01-01 00:00:00")   # the note did bump updated_at
        dais(self.root, "fire", "d-1", "claim", "--by", "engineer")
        self.assertNotEqual(q(self.root, "SELECT state_entered_at FROM tasks WHERE id='d-1'")[0],
                            "2026-01-01 00:00:00")


class TestProbeLoopCooldown(CliTest):
    """design/probe-loop-cooldown.md option C (plan 2.2): progress is a NET STATUS DIFF of the
    role's dispatch-set between launch (runs.dispatch_fp) and now, not a verb count. A claim
    that a system interrupt reverted is a no-op; a claim that stuck is progress."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        with open(os.path.join(self.root, "projects", "demo", "agents", "lead.md"), "w") as f:
            f.write("---\ntrigger: none\n---\npersona\n")   # keep the cadence lead out of the way
        dais(self.root, "task", "add", "demo", "Build it", "--id", "d-1", "--status", "ready")

    def _fp(self, role="engineer"):
        return subprocess.run([os.path.join(self.root, "harness", "router.py"), "--dispatch-set",
                               self.root, "demo", role], capture_output=True, text=True).stdout.strip()

    def _seed_run(self, mins_ago, fp, verb="claim"):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,started_at,ended_at,status,dispatch_fp) VALUES('demo',"
                     "'engineer',datetime('now','-%d minutes'),datetime('now','-%d minutes'),'succeeded',?)"
                     % (mins_ago, mins_ago), (fp,))
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        if verb:
            conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(?,?,?)", (rid, "d-1", verb))
        conn.commit(); conn.close()

    def test_a_reverted_claim_is_a_no_op(self):
        # the run fired `claim` (the old signal for progress) but the task is back at `ready`:
        # the dispatch-set reads exactly as it did at launch -> throttled
        self._seed_run(5, self._fp(), verb="claim")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run engineer", r.stdout)

    def test_a_claim_that_stuck_is_progress(self):
        fp_at_launch = self._fp()                          # d-1|ready
        self._seed_run(5, fp_at_launch, verb="claim")
        dais(self.root, "fire", "d-1", "claim", "--by", "engineer")   # now d-1|doing
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run engineer", r.stdout)     # doing dispatches the engineer again

    def test_pre_migration_rows_keep_the_verb_check(self):
        self._seed_run(5, None, verb="claim")             # no fingerprint recorded
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run engineer", r.stdout)     # a claim verb still reads as progress

    def test_two_reverted_claims_stall_the_role(self):
        self._seed_run(50, self._fp(), verb="claim")
        self._seed_run(5, self._fp(), verb="claim")
        r = dais(self.root, "tick", "demo")
        self.assertNotIn("running engineer", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.root, "projects", "demo", ".stalled-engineer")))
        self.assertIn("STALL", open(os.path.join(self.root, "projects", ".watch.log")).read())


class TestParallelDefault(CliTest):
    """plan 3.5: dais.yaml `parallel: N` is the loop's default width (a launchd tick and
    `dais watch` with no explicit N both read it); DAIS_MAX_PARALLEL still wins."""

    def test_dry_run_reports_the_pool_width(self):
        dais(self.root, "scaffold", "demo")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("pool width 1", r.stdout)
        with open(os.path.join(self.root, "dais.yaml"), "a") as f:
            f.write("parallel: 3\n")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("pool width 3", r.stdout)
        r = dais(self.root, "tick", "demo", "--dry-run", env={"DAIS_MAX_PARALLEL": "2"})
        self.assertIn("pool width 2", r.stdout)


class TestNotify(CliTest):
    """plan 4.4: dais.yaml `notify: <command>` gets a message on stdin once per task when it
    newly parks in NEEDS YOU (or is held over budget), and once per day for a spent budget."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        with open(os.path.join(self.root, "projects", "demo", "agents", "lead.md"), "w") as f:
            f.write("---\ntrigger: none\n---\npersona\n")
        self.log = os.path.join(self.root, "notify.log")
        with open(os.path.join(self.root, "dais.yaml"), "a") as f:
            f.write("notify: cat >> %s\n" % self.log)

    def _lines(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []

    def test_a_new_gate_notifies_once(self):
        dais(self.root, "task", "add", "demo", "Approve me", "--id", "d-1", "--status", "proposal_review")
        dais(self.root, "tick", "demo")
        lines = self._lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("d-1", lines[0]); self.assertIn("Approve me", lines[0]); self.assertIn("proposal review", lines[0])
        dais(self.root, "tick", "demo")                             # same gate, same state: silent
        self.assertEqual(len(self._lines()), 1)
        dais(self.root, "fire", "d-1", "request_changes")           # leaves the gate…
        dais(self.root, "task", "set", "d-1", "--notes", "x")
        dais(self.root, "fire", "d-1", "submit", "--by", "lead")    # …and comes back: a new arrival
        dais(self.root, "tick", "demo")
        self.assertEqual(len(self._lines()), 2)

    def test_dry_run_and_no_notify_key_are_silent(self):
        dais(self.root, "task", "add", "demo", "Approve me", "--id", "d-1", "--status", "proposal_review")
        dais(self.root, "tick", "demo", "--dry-run")
        self.assertEqual(self._lines(), [])
        os.remove(os.path.join(self.root, "dais.yaml"))
        dais(self.root, "tick", "demo")
        self.assertEqual(self._lines(), [])

    def test_notify_test_sends_a_message(self):
        r = dais(self.root, "notify", "test", "hello from dais")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hello from dais", self._lines()[0])
        os.remove(os.path.join(self.root, "dais.yaml"))
        r = dais(self.root, "notify", "test", "x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("notify:", r.stdout + r.stderr)


class TestDispatcherHygiene(CliTest):
    """Plan 2.1: a dry-run tick is read-only, and only one tick runs at a time."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        # the template lead is a never-run cadence role: a real tick would try to launch it.
        # Dormant, so an empty board means "nothing to run".
        with open(os.path.join(self.root, "projects", "demo", "agents", "lead.md"), "w") as f:
            f.write("---\ntrigger: none\n---\npersona\n")

    def test_dry_run_leaves_a_mismatched_stall_marker_alone(self):
        sm = os.path.join(self.root, "projects", "demo", ".stalled-lead")
        with open(sm, "w") as f:
            f.write("t-9|proposed\n")                      # a world that no longer exists
        dais(self.root, "tick", "demo", "--dry-run")
        self.assertTrue(os.path.exists(sm), "dry-run must not clear stall markers")
        dais(self.root, "tick", "demo")
        self.assertFalse(os.path.exists(sm), "a real tick clears a marker whose world changed")

    def test_a_live_tick_makes_the_next_one_step_aside(self):
        lock = os.path.join(self.root, "projects", ".tick.lock")
        os.makedirs(lock)
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(holder.kill)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write("%d\n" % holder.pid)
        r = dais(self.root, "tick", "demo")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("another tick", r.stdout)
        self.assertTrue(os.path.isdir(lock))               # the holder's lock is untouched

    def test_a_stale_tick_lock_is_reclaimed_and_released(self):
        lock = os.path.join(self.root, "projects", ".tick.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write("999999\n")                              # a pid that is not alive
        r = dais(self.root, "tick", "demo")
        self.assertNotIn("another tick", r.stdout)
        self.assertIn("nothing to run", r.stdout)
        self.assertFalse(os.path.exists(lock))              # released on exit


class TestIdleCheckTick(CliTest):
    """End to end through `dais tick --dry-run`: a cadence lead whose interval elapsed is skipped
    while the board is exactly as it left it, and runs once anything on the board moves."""

    def setUp(self):
        super().setUp()
        import sqlite3
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "parked", "--id", "d-1", "--status", "approved")   # not reactive
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,started_at,ended_at,status) VALUES('demo','lead',"
                     "datetime('now','-30 hours'),datetime('now','-30 hours'),'succeeded')")
        conn.commit(); conn.close()
        fp = subprocess.run([os.path.join(self.root, "harness", "router.py"), "--board-fingerprint",
                             self.root, "demo"], capture_output=True, text=True).stdout.strip()
        with open(os.path.join(self.root, "projects", "demo", ".cadence-lead"), "w") as f:
            f.write(fp + "\n")

    def test_unchanged_board_skips_the_lead_and_journals_why(self):
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run lead", r.stdout)
        self.assertIn("nothing eligible", r.stdout)
        r = dais(self.root, "tick", "demo")                       # a real tick journals the reason
        self.assertIn("idle-check", open(os.path.join(self.root, "projects", ".watch.log")).read())

    def test_a_priority_change_wakes_the_lead(self):
        dais(self.root, "task", "set", "d-1", "--priority", "high")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run lead", r.stdout)


class TestNoopThrottle(CliTest):
    """A role whose LAST run succeeded recently but touched no tasks is NOT re-dispatched — the
    reactive no-progress throttle. Without it the machine hot-loops a role that keeps declining
    to act (a lead dispatched every tick for a proposed task it won't submit burned ~12 runs in
    20 minutes). An older no-op, or a run that touched tasks, dispatches normally."""

    def _seed_run(self, mins_ago, touched=None, verb="touch"):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,started_at,ended_at,status) "
                     "VALUES('demo','lead',datetime('now','-%d minutes'),"
                     "datetime('now','-%d minutes'),'succeeded')" % (mins_ago, mins_ago))
        if touched:
            rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(?,?,?)",
                         (rid, touched, verb))
        conn.commit(); conn.close()

    def test_recent_noop_suppresses_redispatch(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "An initiative", "--id", "d-1")   # proposed -> lead
        self._seed_run(5)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run lead", r.stdout)
        self.assertIn("nothing eligible", r.stdout)

    def test_old_noop_dispatches_again(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "An initiative", "--id", "d-1")
        self._seed_run(120)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run lead", r.stdout)

    def test_throttled_role_does_not_starve_the_project(self):
        # the lead is throttled (recent no-op) but READY work exists for the engineer — the tick
        # must fall through to the engineer, not skip the whole project for the cooldown.
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "An initiative", "--id", "d-1")            # proposed -> lead
        dais(self.root, "task", "add", "demo", "Build it", "--id", "d-2", "--status", "ready")
        self._seed_run(5)
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run engineer", r.stdout)
        self.assertNotIn("WOULD run lead", r.stdout)

    def test_run_that_acted_does_not_throttle(self):
        # a real state change (verb=claim/submit/…) is progress — re-dispatch normally
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "An initiative", "--id", "d-1")
        self._seed_run(5, touched="d-1", verb="submit")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("WOULD run lead", r.stdout)

    def test_notes_only_run_still_throttles(self):
        # 'touch' (metadata-only: notes/pr) is NOT progress — four HOLD audits that each wrote
        # notes defeated the throttle in the cou-21 incident. Metadata must not reset it.
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "An initiative", "--id", "d-1")
        self._seed_run(5, touched="d-1", verb="touch")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertNotIn("WOULD run lead", r.stdout)


class TestStallEscalationVerifyGate(CliTest):
    """A role's 2nd consecutive touch-only run PERMANENTLY parks it (`.stalled-<role>`, fa459de)
    — correct for a truly stuck task, but a task legitimately WAITING in a multi-run state (e.g.
    'releasing' on an async EAS/CI build) can ONLY report progress via a note between polls, and
    its dispatch-set fingerprint (id|status) never changes while it's correctly waiting — so the
    marker never self-clears (observed 2026-07-17: `dais tick` said "nothing to run", only
    `dais start` worked). A verify:-guarded exit edge is the machine's OWN signal that a state may
    need several polling runs; the escalation must skip it there (the 45m throttle still paces
    the polling) while still protecting a state WITHOUT that signal (the original fa459de case)."""

    def _add_verify_guard(self):
        mp = os.path.join(self.root, "projects", "demo", "machine.json")
        with open(mp) as fh:
            m = json.load(fh)
        for e in m["edges"]:
            if e["from"] == "releasing" and e["verb"] == "shipped":
                e["guards"] = e.get("guards", []) + ["verify:migrations_live"]
        with open(mp, "w") as fh:
            json.dump(m, fh)

    def _seed_two_touch_only_runs(self, task_id):
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        for mins_ago in (20, 10):
            conn.execute("INSERT INTO runs(project,agent,started_at,ended_at,status) "
                         "VALUES('demo','engineer',datetime('now','-%d minutes'),"
                         "datetime('now','-%d minutes'),'succeeded')" % (mins_ago, mins_ago))
            rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(?,?,'touch')",
                         (rid, task_id))
        conn.commit(); conn.close()

    def _stall_marker(self):
        return os.path.join(self.root, "projects", "demo", ".stalled-engineer")

    def test_verify_gated_state_is_never_permanently_stalled(self):
        dais(self.root, "scaffold", "demo")
        self._add_verify_guard()
        dais(self.root, "task", "add", "demo", "Release", "--id", "d-1", "--status", "releasing")
        self._seed_two_touch_only_runs("d-1")
        r = dais(self.root, "tick", "demo")     # real tick: writes markers, but has nothing to launch
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)   # idle — engineer is 45m-throttled
        self.assertFalse(os.path.exists(self._stall_marker()),
                         "a verify-gated state must never get a permanent stall marker")

    def test_non_verify_gated_state_still_stalls(self):
        # protects the ORIGINAL fa459de fix: a state with no structural verify signal keeps the
        # existing protection (an agent can't dodge the stall by writing notes).
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "Release", "--id", "d-1", "--status", "releasing")
        self._seed_two_touch_only_runs("d-1")
        r = dais(self.root, "tick", "demo")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self._stall_marker()),
                        "a non-verify-gated stuck task should still escalate to a stall marker")


class TestDbLifecycle(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # NOTE: deliberately do NOT run `dais init` — exercise auto-init.

    def test_autoinit_on_first_use(self):
        self.assertFalse(os.path.exists(os.path.join(self.root, "dais.db")))
        r = dais(self.root, "tasks", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("no such table", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, "dais.db")))

    def test_schema_version_table_exists(self):
        dais(self.root, "init")
        # querying the table must not error
        self.assertIsNotNone(q(self.root, "SELECT count(*) FROM schema_version"))

    def test_migration_applies_once(self):
        mig = os.path.join(self.root, "harness", "migrations", "050_add_test_col.sql")
        with open(mig, "w") as fh:
            fh.write("ALTER TABLE tasks ADD COLUMN test_col TEXT;\n")
        self.assertEqual(dais(self.root, "init").returncode, 0)
        # column exists now
        cols = [r[1] for r in sqlite3.connect(
            os.path.join(self.root, "dais.db")).execute("PRAGMA table_info(tasks)")]
        self.assertIn("test_col", cols)
        # recorded once; a second init is idempotent (no duplicate-column error)
        self.assertEqual(q(self.root, "SELECT count(*) FROM schema_version "
                                      "WHERE filename='050_add_test_col.sql'")[0], 1)
        self.assertEqual(dais(self.root, "init").returncode, 0)
        self.assertEqual(q(self.root, "SELECT count(*) FROM schema_version "
                                      "WHERE filename='050_add_test_col.sql'")[0], 1)


class TestMigrateCommand(CliTest):
    """`dais migrate` is the explicit way to apply pending migrations on an
    EXISTING db (normal commands only auto-init when the db file is absent), and
    each migration applies inside a transaction that rolls back on error."""

    def _has_col(self, col):
        return q(self.root, "SELECT COUNT(*) FROM pragma_table_info('tasks') "
                            "WHERE name='%s'" % col)[0]

    def test_migrate_applies_pending_and_normal_commands_do_not(self):
        mig = os.path.join(self.root, "harness", "migrations", "001_add_test_col.sql")
        with open(mig, "w") as fh:
            fh.write("ALTER TABLE tasks ADD COLUMN test_col TEXT;\n")
        # (a) a normal command hits the db but must NOT apply the migration
        #     (db already exists, so db() never re-inits).
        dais(self.root, "task", "add", "demo", "A task", "--id", "d-1")
        self.assertEqual(self._has_col("test_col"), 0,
                         "normal commands must not apply migrations on an existing db")
        # (b) dais migrate applies it and records it exactly once.
        r = dais(self.root, "migrate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._has_col("test_col"), 1)
        self.assertEqual(q(self.root, "SELECT COUNT(*) FROM schema_version "
                                      "WHERE filename='001_add_test_col.sql'")[0], 1)

    def test_failed_migration_rolls_back_and_is_not_recorded(self):
        mig = os.path.join(self.root, "harness", "migrations", "002_partial.sql")
        with open(mig, "w") as fh:
            fh.write("ALTER TABLE tasks ADD COLUMN partial_col TEXT;\n"
                     "INSERT INTO no_such_table_xyz VALUES(1);\n")
        dais(self.root, "migrate")
        # the 1st statement must NOT survive the 2nd statement's error
        self.assertEqual(self._has_col("partial_col"), 0,
                         "a partially-applied migration must roll back atomically")
        # and a rolled-back migration must not be recorded as applied
        self.assertEqual(q(self.root, "SELECT COUNT(*) FROM schema_version "
                                      "WHERE filename='002_partial.sql'")[0], 0)


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _need(self, tool, hint, path):
        # call the bash helper directly with a controlled PATH
        script = ('source "%s/harness/lib.sh"; need %s "%s"'
                  % (self.root, tool, hint))
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": path, "DAIS_ROOT": self.root})

    def test_missing_tool_fails_loud(self):
        r = self._need("definitely_not_a_real_tool", "install it", _PREFLIGHT_PATH)
        self.assertEqual(r.returncode, 127)
        self.assertIn("definitely_not_a_real_tool", r.stderr)
        self.assertIn("install it", r.stderr)

    def test_present_tool_is_noop(self):
        r = self._need("sh", "n/a", _PREFLIGHT_PATH)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")


class TestIsCapped(unittest.TestCase):
    """is_capped <log> [provider] — provider-aware usage-limit detection (lib.sh)."""

    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        d = tempfile.mkdtemp(prefix="dais-is-capped-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.log = os.path.join(d, "run.log")

    def _lib(self, cmd):
        # call the bash helper directly with a controlled PATH, mirroring TestPreflight._need
        script = 'source "%s/harness/lib.sh"; %s' % (self.root, cmd)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": _PREFLIGHT_PATH, "DAIS_ROOT": self.root})

    def test_is_capped_openai_patterns(self):
        for msg in ("You've hit your usage limit. Try again later.",
                    "Rate limit reached for gpt-5.2",
                    "insufficient_quota: your credit balance is too low"):
            with open(self.log, "w") as f:
                f.write(msg + "\n")
            r = self._lib("is_capped '%s' openai" % self.log)
            self.assertEqual(r.returncode, 0, msg)

    def test_is_capped_anthropic_unchanged(self):
        with open(self.log, "w") as f:
            f.write("You've hit your session limit — resets at 3pm\n")
        self.assertEqual(self._lib("is_capped '%s'" % self.log).returncode, 0)
        self.assertEqual(self._lib("is_capped '%s' anthropic" % self.log).returncode, 0)

    def test_is_capped_openai_does_not_match_anthropic_only_phrasing(self):
        # "rate limit reached" / insufficient_quota are openai-only patterns; an
        # unrelated line must not false-positive under either provider.
        with open(self.log, "w") as f:
            f.write("just a normal line about rate limits in general\n")
        self.assertNotEqual(self._lib("is_capped '%s' openai" % self.log).returncode, 0)

    def test_is_capped_api_metered_patterns_both_providers(self):
        for provider in ("anthropic", "openai"):
            with open(self.log, "w") as f:
                f.write("Error: 429 Too Many Requests\n")
            self.assertEqual(self._lib("is_capped '%s' %s" % (self.log, provider)).returncode, 0)


class TestRemovedLegacyVerbs(CliTest):
    """approve/handoff/backlog were legacy status pokes that bypassed the machine (and could
    strand a task in a state its machine doesn't have). They exit nonzero, change NOTHING,
    and point at the machine path (dais edges / dais fire)."""

    def test_approve_is_removed_and_changes_nothing(self):
        dais(self.root, "task", "add", "demo", "Idea", "--id", "d-1", "--status", "proposed")
        r = dais(self.root, "approve", "d-1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("dais fire", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-1'")[0], "proposed")

    def test_handoff_is_removed_and_changes_nothing(self):
        dais(self.root, "task", "add", "demo", "Idea", "--id", "d-2", "--status", "ready")
        r = dais(self.root, "handoff", "d-2", "qa")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("dais fire", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-2'")[0], "ready")

    def test_backlog_is_removed(self):
        r = dais(self.root, "backlog", "demo")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("dais status", r.stdout + r.stderr)


class TestRepoPath(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "projects", "demo"))

    def _repo_path(self, repo_value, env=None):
        yaml = os.path.join(self.root, "projects", "demo", "project.yaml")
        with open(yaml, "w") as fh:
            fh.write("project: demo\nrepo: %s\n" % repo_value)
        # Pin DAIS_HOME too: resolution now keys on cwd, but this is a repo_path unit
        # test (project.yaml lives under self.root), so make the workspace explicit.
        e = {"DAIS_ROOT": self.root, "DAIS_HOME": self.root,
             "PATH": os.environ["PATH"], "HOME": "/home/x"}
        if env:
            e.update(env)
        r = subprocess.run(["bash", "-c",
                            'source "%s/harness/lib.sh"; repo_path demo' % self.root],
                           capture_output=True, text=True, env=e)
        return r.stdout.strip()

    def test_absolute_unchanged(self):
        self.assertEqual(self._repo_path("/srv/code/demo"), "/srv/code/demo")

    def test_tilde_expands_home(self):
        self.assertEqual(self._repo_path("~/code/demo"), "/home/x/code/demo")

    def test_relative_resolves_against_base(self):
        self.assertEqual(self._repo_path("demo", env={"DAIS_AGENT_REPOS": "/work"}),
                         "/work/demo")

    def test_relative_default_base_is_parent_of_workspace(self):
        # default base = parent of the WORKSPACE (DAIS_HOME), NOT the install dir (DAIS_ROOT) —
        # so a packaged install (DAIS_ROOT in a read-only Cellar) still resolves repos next to
        # the workspace. Prove it by pointing DAIS_ROOT at a Cellar-like path it must ignore.
        expected = os.path.join(os.path.dirname(self.root), "demo")
        got = self._repo_path("demo",
                              env={"DAIS_ROOT": "/opt/homebrew/Cellar/dais/9.9.9/libexec"})
        self.assertEqual(got, expected)


class TestPcfgBlockScalar(unittest.TestCase):
    """lib.sh's pcfg() reads project.yaml line-by-line — a YAML block-scalar indicator
    (`key: >-` etc.) as the whole value used to come back as the literal indicator text instead
    of the folded paragraph (a real incident: a `stage_goal: >-` folded scalar reached an
    agent's prompt as the string '>-'). pcfg must fold in the following more-indented lines."""

    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "projects", "demo"))

    def _pcfg(self, yaml_body, key="stage_goal"):
        yaml = os.path.join(self.root, "projects", "demo", "project.yaml")
        with open(yaml, "w") as fh:
            fh.write(yaml_body)
        e = {"DAIS_ROOT": self.root, "DAIS_HOME": self.root, "PATH": os.environ["PATH"]}
        r = subprocess.run(["bash", "-c",
                            'source "%s/harness/lib.sh"; pcfg demo %s' % (self.root, key)],
                           capture_output=True, text=True, env=e)
        return r.stdout.strip()

    def test_plain_single_line_value_unchanged(self):
        got = self._pcfg("project: demo\nstage_goal: ship the thing\nrepo: x\n")
        self.assertEqual(got, "ship the thing")

    def test_folded_block_scalar_is_joined(self):
        got = self._pcfg(
            "project: demo\n"
            "stage_goal: >-\n"
            "  Ship the launch-week fixes and keep the\n"
            "  release lane green.\n"
            "repo: x\n")
        self.assertEqual(got, "Ship the launch-week fixes and keep the release lane green.")
        self.assertNotEqual(got, ">-")

    def test_literal_block_scalar_is_joined_too(self):
        # space-join both folded (>) and literal (|) — good enough for these single-paragraph
        # fields per the task's own scope (no real multi-paragraph project.yaml field exists).
        got = self._pcfg(
            "project: demo\n"
            "stage_goal: |\n"
            "  first line\n"
            "  second line\n"
            "repo: x\n")
        self.assertEqual(got, "first line second line")

    def test_block_scalar_stops_at_next_top_level_key(self):
        got = self._pcfg(
            "project: demo\n"
            "stage_goal: >-\n"
            "  only this paragraph\n"
            "repo: x\n"
            "priority: 5\n")
        self.assertEqual(got, "only this paragraph")

    def test_missing_key_is_empty(self):
        self.assertEqual(self._pcfg("project: demo\n", key="nope"), "")


class TestRunAgentRepoPath(CliTest):
    def test_relative_repo_resolves_via_repo_path(self):
        # A scaffolded project ships a RELATIVE `repo:` (the template default).
        # run-agent.sh must resolve it through repo_path (against DAIS_AGENT_REPOS),
        # not treat the bare value as a path. It fails fast at the `[ -d "$REPO" ]`
        # guard (before any claude call), so the error reveals the resolved path.
        dais(self.root, "scaffold", "demo")
        base = tempfile.mkdtemp(prefix="dais-repos-")  # does NOT contain `demo`
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_AGENT_REPOS": base})
        r = subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"),
                            "demo", "engineer"],
                           capture_output=True, text=True, env=e, cwd=self.root)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("repo not found: %s" % os.path.join(base, "demo"), out)


class TestPerRoleModelOverride(CliTest):
    """project.yaml `model_<role>:` / `effort_<role>:` override the project-wide `model:` /
    `effort:` for that role only; roles without an override keep the project default. Asserted
    via the DAIS_SHOW_CONFIG=1 debug seam (prints the resolved model/effort, exits pre-claude
    and pre-repo-guard, so no repo scaffolding is needed). This class also covers frontmatter
    precedence, provider/auth/access resolution, and persona-frontmatter stripping (the latter
    via DAIS_SHOW_PROMPT, which sits past the repo-existence guard, so setUp gives it a repo)."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")   # template default: model claude-opus-4-8, effort high
        with open(os.path.join(self.root, "projects", "demo", "project.yaml"), "a") as fh:
            fh.write("model_qa: claude-haiku-4-5\neffort_qa: low\n")
        self.repo_base = tempfile.mkdtemp(prefix="dais-repos-")
        self.addCleanup(shutil.rmtree, self.repo_base, ignore_errors=True)
        os.makedirs(os.path.join(self.repo_base, "demo"))

    def _show_config(self, agent):
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_SHOW_CONFIG": "1"})
        r = subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", agent],
                           capture_output=True, text=True, env=e, cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def _show_prompt(self, agent):
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": self.repo_base, "DAIS_SHOW_PROMPT": "1"})
        r = subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", agent],
                           capture_output=True, text=True, env=e, cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def _run_agent(self, agent, env=None, stdin_text=None):
        # like _show_config, but WITHOUT DAIS_SHOW_CONFIG — the run must reach the
        # auth:api preflight (which sits after the config seam), not stop at it.
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": self.repo_base})
        if env:
            e.update(env)
        return subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", agent],
                              capture_output=True, text=True, env=e, cwd=self.root,
                              input=stdin_text)

    def test_role_override_beats_project_default(self):
        qa = self._show_config("qa")
        self.assertIn("model=claude-haiku-4-5", qa)
        self.assertIn("effort=low", qa)
        eng = self._show_config("engineer")        # no override -> project default untouched
        self.assertIn("model=claude-opus-4-8", eng)
        self.assertIn("effort=high", eng)

    def test_project_default_without_override_keys(self):
        out = self._show_config("engineer")
        self.assertIn("model=claude-opus-4-8", out)
        self.assertIn("effort=high", out)

    def test_frontmatter_model_beats_suffix_key(self):
        # project.yaml says model_qa: claude-haiku-4-5 (set in setUp); frontmatter wins
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent) as f:
            body = f.read()
        with open(agent, "w") as f:
            f.write("---\nmodel: claude-sonnet-5\n---\n" + body)
        out = self._show_config("qa")
        self.assertIn("model=claude-sonnet-5", out)

    def test_show_config_includes_provider_auth_access(self):
        out = self._show_config("qa")
        self.assertIn("provider=anthropic", out)
        self.assertIn("auth=subscription", out)
        self.assertIn("access=", out)

    def test_frontmatter_stripped_from_prompt(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nmodel: claude-sonnet-5\n---\nPERSONA-BODY-MARKER\n")
        out = self._show_prompt("qa")          # DAIS_SHOW_PROMPT seam + role file dump
        self.assertNotIn("model: claude-sonnet-5", out)

    def test_api_auth_without_key_fails_fast(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nauth: api\n---\npersona\n")
        r = self._run_agent("qa", env={"ANTHROPIC_API_KEY": ""})   # ensure absent
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ANTHROPIC_API_KEY", r.stdout + r.stderr)

    def test_env_file_supplies_key(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nauth: api\n---\npersona\n")
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write("ANTHROPIC_API_KEY=sk-test-not-real\n")
        out = self._show_config("qa")               # preflight passes; config seam prints
        self.assertIn("auth=api", out)

    def test_env_file_without_a_trailing_newline_still_supplies_the_key(self):
        # bug 6 (plan 2.4): `while read` drops a final line with no newline — the common
        # `printf 'KEY=…' > .env` shape silently lost the key and the preflight refused to run
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nauth: api\n---\npersona\n")
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write("ANTHROPIC_API_KEY=sk-test-not-real")          # no trailing newline
        # a REAL run (not the config seam, which exits before the preflight) against a fake claude
        argv = os.path.join(self.root, "claude-argv")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(argv)),
                                       "HOME": self._fake_home(), "ANTHROPIC_API_KEY": ""})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("is not set", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(argv), "the run must reach the CLI")

    def test_lock_slot_claim_skips_a_live_peer_and_reclaims_a_dead_one(self):
        # bug 7 (plan 2.4): the claim is now atomic (noclobber create); behaviorally, a live
        # holder still means "skip" and a dead one is reclaimed
        argv = os.path.join(self.root, "claude-argv")
        lock = os.path.join(self.root, "projects", "demo", ".lock-qa")
        holder = subprocess.Popen(["sleep", "30"]); self.addCleanup(holder.kill)
        with open(lock, "w") as f:
            f.write("%d\n" % holder.pid)
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(argv)),
                                       "HOME": self._fake_home()})
        self.assertIn("skipping", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(argv))
        self.assertEqual(open(lock).read().strip(), str(holder.pid))   # the peer's lock is untouched
        with open(lock, "w") as f:
            f.write("999999\n")                                         # dead holder
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(argv)),
                                       "HOME": self._fake_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(argv))
        self.assertFalse(os.path.exists(lock))                          # released on exit

    def test_fallback_keeps_the_capped_attempts_log(self):
        # bug 8 (plan 2.4): the fallback attempt truncated the log, so the capped primary
        # attempt left no trace; keep it beside the final log
        argv = os.path.join(self.root, "claude-argv")
        fake = ('printf "%s\\n" "$@" >> "' + argv + '"\n'
                'm=""; while [ $# -gt 0 ]; do [ "$1" = "--model" ] && m="$2"; shift; done\n'
                'if [ "$m" = "claude-fable-5" ]; then\n'
                '  echo \'{"type":"assistant","message":{"content":[{"type":"text","text":"You\\u0027ve hit your usage limit for Fable"}]}}\'\n'
                'else\n'
                '  echo \'{"type":"assistant","message":{"content":[{"type":"text","text":"fallback working"}]}}\'\n'
                '  echo \'{"type":"result","subtype":"success","num_turns":1,"usage":{"input_tokens":5,"output_tokens":1}}\'\n'
                'fi\n')
        self._set_role("qa", "model: claude-fable-5\nfallback_model: claude-opus-5\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=fake), "HOME": self._fake_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = q(self.root, "SELECT status, model, log_path FROM runs ORDER BY id DESC LIMIT 1")
        self.assertEqual((row[0], row[1]), ("succeeded", "claude-opus-5"))
        self.assertIn("fallback working", open(row[2]).read())
        kept = row[2] + ".capped-claude-fable-5"
        self.assertTrue(os.path.exists(kept), "the capped attempt's log must be kept")
        self.assertIn("usage limit", open(kept).read())

    def test_unknown_provider_fails_with_named_error(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nprovider: gemini\n---\npersona\n")
        out = self._show_config("qa")
        self.assertIn("provider=gemini", out)      # resolution passes it through
        r = self._run_agent("qa")                  # the run itself fails, named
        self.assertIn("no provider pack 'gemini'", r.stdout + r.stderr)
        self.assertIn("anthropic", r.stdout + r.stderr)   # the packs it does have

    # --- provider packs (plan 5.1): a third provider is a directory, not a code change ---------
    def _fake_pack(self, name="fakeprov"):
        d = os.path.join(self.root, "harness", "providers", name)
        os.makedirs(d)
        with open(os.path.join(d, "pack.json"), "w") as f:
            f.write(json.dumps({"cli": "fakecli", "key_var": "FAKE_API_KEY", "default_model": "fake-1"}))
        with open(os.path.join(d, "caps.txt"), "w") as f:
            f.write("fake quota exhausted\n")
        with open(os.path.join(d, "run.sh"), "w") as f:
            f.write('provider_run(){\n  fakecli "$MODEL" "$WORKDIR" "$STANDING" 2>&1 | python3 -u "$DAIS_ROOT/harness/fmt-stream.py" "$LOG" --provider fakeprov\n}\n')
        with open(os.path.join(d, "stream.py"), "w") as f:
            f.write("def handle(e, emit, acc):\n"
                    "    if e.get('type') == 'say': emit('  💬 ' + e.get('text', ''), 'cyan')\n"
                    "    elif e.get('type') == 'done': acc(input_tokens=e.get('tokens', 0), output_tokens=1, turns=1); emit('  ✓ done', 'green')\n")
        b = tempfile.mkdtemp(prefix="dais-bin-"); self.addCleanup(shutil.rmtree, b, ignore_errors=True)
        import sys as _sys
        os.symlink(_sys.executable, os.path.join(b, "python3"))
        for t in ("sqlite3", "git"):
            os.symlink(shutil.which(t), os.path.join(b, t))
        with open(os.path.join(b, "fakecli"), "w") as f:
            f.write('#!/bin/bash\necho "{\\"type\\":\\"say\\",\\"text\\":\\"hello from $1 in $2\\"}"\necho "{\\"type\\":\\"done\\",\\"tokens\\":777}"\n')
        os.chmod(os.path.join(b, "fakecli"), 0o755)
        return "%s:/usr/bin:/bin" % b

    def test_a_dropped_in_pack_runs_end_to_end(self):
        path = self._fake_pack()
        self._set_role("qa", "provider: fakeprov\n")
        out = self._show_config("qa")
        self.assertIn("model=fake-1", out)                # the pack's default model
        r = self._run_agent("qa", env={"PATH": path})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = q(self.root, "SELECT status, provider, model, input_tokens FROM runs ORDER BY id DESC LIMIT 1")
        self.assertEqual(tuple(row), ("succeeded", "fakeprov", "fake-1", 777))
        log = open(q(self.root, "SELECT log_path FROM runs ORDER BY id DESC LIMIT 1")[0]).read()
        self.assertIn("hello from fake-1", log)

    def test_pack_caps_patterns_score_a_capped_run(self):
        path = self._fake_pack()
        # a fakecli that reports its quota message
        b = path.split(":")[0]
        with open(os.path.join(b, "fakecli"), "w") as f:
            f.write('#!/bin/bash\necho "{\\"type\\":\\"say\\",\\"text\\":\\"fake quota exhausted, try later\\"}"\n')
        self._set_role("qa", "provider: fakeprov\n")
        r = self._run_agent("qa", env={"PATH": path})
        self.assertEqual(q(self.root, "SELECT status FROM runs ORDER BY id DESC LIMIT 1")[0], "capped")

    def test_pack_without_its_cli_fails_at_preflight(self):
        self._fake_pack()
        self._set_role("qa", "provider: fakeprov\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin()})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fakecli", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT COUNT(*) FROM runs")[0], 0)

    # --- provider CLI preflight + the codex adapter, exercised with a controlled PATH ---------
    # A bin dir holding ONLY what run-agent needs (the real python3, sqlite3, git) plus an
    # optional fake `codex`; PATH = that dir + /usr/bin:/bin, so the real provider CLIs
    # (Homebrew / ~/.local/bin) are invisible and the test decides what "installed" means.
    def _tmpbin(self, fake_codex=None, fake_claude=None):
        import sys
        b = tempfile.mkdtemp(prefix="dais-bin-")
        self.addCleanup(shutil.rmtree, b, ignore_errors=True)
        os.symlink(sys.executable, os.path.join(b, "python3"))
        for tool in ("sqlite3", "git"):
            os.symlink(shutil.which(tool), os.path.join(b, tool))
        for name, body in (("codex", fake_codex), ("claude", fake_claude)):
            if body is not None:
                p = os.path.join(b, name)
                with open(p, "w") as fh:
                    fh.write("#!/bin/bash\n" + body)
                os.chmod(p, 0o755)
        return "%s:/usr/bin:/bin" % b

    # --- the lean agent profile (plan 1.3), asserted on the claude argv via a fake `claude` ---
    def _fake_claude(self, argv_file):
        return ('printf "%s\\n" "$@" > "' + argv_file + '"\n'
                'echo \'{"type":"result","subtype":"success","num_turns":1,"usage":{"input_tokens":5,"output_tokens":1}}\'\n')

    def _fake_home(self):
        import json
        home = tempfile.mkdtemp(prefix="dais-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"qmd": {"command": "qmd"}, "gbrain": {"command": "gbrain"}}}, f)
        os.makedirs(os.path.join(home, ".claude", "plugins", "cache", "official", "supabase", "1.2.0"))
        return home

    def _claude_argv(self, fm):
        argv = os.path.join(self.root, "claude-argv")
        self._set_role("qa", fm)
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(argv)),
                                       "HOME": self._fake_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return open(argv).read().split("\n")

    def test_lean_profile_strips_user_settings_and_mcp(self):
        import json
        args = self._claude_argv("")                   # default context: lean
        self.assertEqual(args[args.index("--setting-sources") + 1], "project,local")
        self.assertIn("--strict-mcp-config", args)
        cfg = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(cfg["mcpServers"], {})
        self.assertNotIn("--plugin-dir", args)

    def test_lean_profile_allowlists_mcp_and_plugins(self):
        import json
        args = self._claude_argv("mcp: qmd\nplugins: supabase\n")
        cfg = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(list(cfg["mcpServers"]), ["qmd"])
        pd = args[args.index("--plugin-dir") + 1]
        self.assertTrue(pd.endswith(os.path.join("supabase", "1.2.0")), pd)

    def test_full_profile_passes_no_profile_flags(self):
        args = self._claude_argv("context: full\n")
        for flag in ("--setting-sources", "--strict-mcp-config", "--mcp-config", "--plugin-dir"):
            self.assertNotIn(flag, args)

    def test_run_row_records_the_dispatch_fingerprint_at_launch(self):
        # plan 2.2: the role's dispatch-set as it read when the run started (after reconcile)
        dais(self.root, "task", "add", "demo", "review me", "--id", "q-1", "--status", "qa_review")
        fp = subprocess.run([os.path.join(self.root, "harness", "router.py"), "--dispatch-set",
                             self.root, "demo", "qa"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(fp, "q-1|qa_review")
        r = self._run_agent("qa", env={"DAIS_NOOP_RUN": "echo ok"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT dispatch_fp FROM runs ORDER BY id DESC LIMIT 1")[0], fp)

    # --- model / effort tiers by task priority (plan 3.3) ----------------------------------------
    def _tiered_argv(self, fm, task_id, priority):
        argv = os.path.join(self.root, "claude-argv")
        if os.path.exists(argv):
            os.unlink(argv)
        dais(self.root, "task", "add", "demo", "work " + task_id, "--id", task_id, "--status", "qa_review",
             "--priority", priority)
        self._set_role("qa", fm)
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(argv)),
                                       "HOME": self._fake_home(), "DAIS_TASK_ID": task_id})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = open(argv).read().split("\n")
        return args[args.index("--model") + 1], (args[args.index("--effort") + 1] if "--effort" in args else "")

    def test_model_and_effort_follow_the_pinned_tasks_priority(self):
        fm = ("model: claude-opus-5\neffort: medium\n"
              "model_by_priority: critical=claude-fable-5, low=claude-haiku-4-5\n"
              "effort_by_priority: critical=high, low=low\n")
        self.assertEqual(self._tiered_argv(fm, "t-crit", "critical"), ("claude-fable-5", "high"))
        self.assertEqual(self._tiered_argv(fm, "t-low", "low"), ("claude-haiku-4-5", "low"))
        self.assertEqual(self._tiered_argv(fm, "t-med", "medium"), ("claude-opus-5", "medium"))  # no tier: the role's own
        self.assertEqual(q(self.root, "SELECT model FROM runs ORDER BY id DESC LIMIT 1")[0], "claude-opus-5")

    def test_tiers_apply_project_wide_from_project_yaml(self):
        with open(os.path.join(self.root, "projects", "demo", "project.yaml"), "a") as f:
            f.write("model_by_priority: critical=claude-fable-5\n")
        self.assertEqual(self._tiered_argv("", "t-crit", "critical")[0], "claude-fable-5")

    # --- session resume (plan 3.2) -----------------------------------------------------------
    def _prior_run(self, task_id, hours_ago=1, status="succeeded", session="sess-abc", agent="qa"):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.root, "dais.db"))
        conn.execute("INSERT INTO runs(project,agent,task_id,status,started_at,ended_at,session_id,provider) "
                     "VALUES('demo',?,?,?,datetime('now','-%d hours'),datetime('now','-%d hours'),?,'anthropic')"
                     % (hours_ago, hours_ago), (agent, task_id, status, session))
        conn.commit(); conn.close()

    def _resume_argv(self, fm="", task_id="d-1"):
        argv = os.path.join(self.root, "claude-argv")
        if os.path.exists(argv):
            os.unlink(argv)
        self._set_role("qa", fm)
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(argv)),
                                       "HOME": self._fake_home(), "DAIS_TASK_ID": task_id})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = open(argv).read().split("\n")
        return args, open(argv).read()       # (argv list, the whole argv text — prompts span lines)

    def test_same_role_same_task_resumes_the_recent_session(self):
        dais(self.root, "task", "add", "demo", "review me", "--id", "d-1", "--status", "qa_review")
        self._prior_run("d-1")
        args, prompt = self._resume_argv()
        self.assertEqual(args[args.index("--resume") + 1], "sess-abc")
        self.assertIn("resuming", prompt.lower())
        self.assertIn("d-1", prompt)
        self.assertNotIn("Workspace context", prompt)      # the short continuation, not the full standing
        self.assertNotIn("FIRST read", prompt)

    def test_no_resume_when_stale_failed_other_task_or_off(self):
        dais(self.root, "task", "add", "demo", "review me", "--id", "d-1", "--status", "qa_review")
        self._prior_run("d-1", hours_ago=10)                 # stale
        args, prompt = self._resume_argv()
        self.assertNotIn("--resume", args); self.assertIn("Workspace context", prompt)
        self._prior_run("d-1", status="failed")             # the last run failed: start fresh
        args, _ = self._resume_argv()
        self.assertNotIn("--resume", args)
        self._prior_run("d-2")                               # a different task's session
        args, _ = self._resume_argv()
        self.assertNotIn("--resume", args)
        self._prior_run("d-1")                               # would resume — but the role opted out
        args, _ = self._resume_argv("resume: off\n")
        self.assertNotIn("--resume", args)
        self._prior_run("d-1")                               # (the off-run itself recorded no session)
        args, _ = self._resume_argv()                        # and does, by default
        self.assertIn("--resume", args)

    # --- the idle check's marker (plan 1.5): a cadence run records the board as it left it ------
    def test_cadence_run_records_the_board_fingerprint(self):
        # coding template: the lead is every:24h. `echo ok` (not `true`): an EMPTY log is scored
        # failed, and only a succeeded cadence run records the board.
        r = self._run_agent("lead", env={"DAIS_NOOP_RUN": "echo ok"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT status FROM runs ORDER BY id DESC LIMIT 1")[0], "succeeded")
        marker = os.path.join(self.root, "projects", "demo", ".cadence-lead")
        self.assertTrue(os.path.exists(marker))
        fp = subprocess.run([os.path.join(self.root, "harness", "router.py"), "--board-fingerprint",
                             self.root, "demo"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(open(marker).read().strip(), fp)
        self.assertTrue(len(fp) >= 12)

    def test_reactive_run_records_no_marker(self):
        r = self._run_agent("engineer", env={"DAIS_NOOP_RUN": "echo ok"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, "projects", "demo", ".cadence-engineer")))

    def test_failed_cadence_run_records_no_marker(self):
        r = self._run_agent("lead", env={"DAIS_NOOP_RUN": "false"})    # the run fails
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, "projects", "demo", ".cadence-lead")))

    # --- 5.2: codex against any OpenAI-compatible endpoint; a proxy base URL for claude -----------
    def _codex_argv(self, fm):
        argv = os.path.join(self.root, "codex-argv")
        if os.path.exists(argv):
            os.unlink(argv)
        fake = ('printf "%s\\n" "$@" > "' + argv + '"\n'
                'echo \'{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"ok"}}\'\n'
                'echo \'{"type":"turn.completed","usage":{}}\'\n')
        self._set_role("qa", "provider: openai\n" + fm)
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_codex=fake)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return open(argv).read().split("\n")

    def test_codex_model_provider_and_base_url_pass_through(self):
        args = self._codex_argv("model: deepseek-chat\nmodel_provider: deepseek\nbase_url: https://api.deepseek.com/v1\nenv_key: DEEPSEEK_API_KEY\n")
        self.assertEqual(args[args.index("-m") + 1], "deepseek-chat")
        self.assertIn('model_provider="deepseek"', args)
        self.assertIn('model_providers.deepseek.base_url="https://api.deepseek.com/v1"', args)
        self.assertIn('model_providers.deepseek.env_key="DEEPSEEK_API_KEY"', args)
        self.assertIn('model_providers.deepseek.name="deepseek"', args)
        args = self._codex_argv("")                                 # unset: nothing added
        self.assertFalse(any(a.startswith("model_provider") or a.startswith("model_providers") for a in args))

    def test_codex_local_provider_flag(self):
        args = self._codex_argv("model: llama3\nlocal: ollama\n")
        self.assertIn("--oss", args)
        self.assertEqual(args[args.index("--local-provider") + 1], "ollama")

    def test_claude_base_url_reaches_the_cli_environment(self):
        argv = os.path.join(self.root, "claude-argv")
        fake = self._fake_claude(argv) + 'echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-unset}" >> "' + argv + '"\n'
        self._set_role("qa", "base_url: http://127.0.0.1:4000\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=fake), "HOME": self._fake_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:4000", open(argv).read())

    # --- 5.3: cross-provider fallback ------------------------------------------------------------
    def test_fallback_can_cross_to_another_provider(self):
        argv = os.path.join(self.root, "codex-argv")
        claude = ('echo \'{"type":"assistant","message":{"content":[{"type":"text","text":"You\\u0027ve hit your usage limit"}]}}\'\n')
        codex = ('printf "%s\\n" "$@" > "' + argv + '"\n'
                 'echo \'{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"codex took it"}}\'\n'
                 'echo \'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":1}}\'\n')
        self._set_role("qa", "model: claude-fable-5\nfallback_provider: openai\nfallback_model: gpt-5.4\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_codex=codex, fake_claude=claude), "HOME": self._fake_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = q(self.root, "SELECT status, provider, model, log_path FROM runs ORDER BY id DESC LIMIT 1")
        self.assertEqual(tuple(row[:3]), ("succeeded", "openai", "gpt-5.4"))
        self.assertIn("codex took it", open(row[3]).read())
        self.assertTrue(os.path.exists(row[3] + ".capped-claude-fable-5"))
        args = open(argv).read().split("\n")
        self.assertEqual(args[args.index("-m") + 1], "gpt-5.4")
        marker = open(os.path.join(self.root, "projects", "demo", ".model-qa.exhausted")).read()
        self.assertIn("claude-fable-5", marker)

    def test_fallback_provider_needs_its_cli_too(self):
        self._set_role("qa", "model: claude-fable-5\nfallback_provider: openai\nfallback_model: gpt-5.4\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(os.path.join(self.root, "a")))})
        self.assertNotEqual(r.returncode, 0)                        # no codex on PATH: say so up front
        self.assertIn("codex", r.stdout + r.stderr)

    # --- budget caps (plan 1.4) ---------------------------------------------------------------
    def test_caps_become_claude_flags_only_when_set(self):
        args = self._claude_argv("max_turns: 25\nmax_budget_usd: 2.50\n")
        self.assertEqual(args[args.index("--max-turns") + 1], "25")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "2.50")
        args = self._claude_argv("")
        self.assertNotIn("--max-turns", args); self.assertNotIn("--max-budget-usd", args)

    def _timeout_run(self, fm, fake_claude=None, fake_codex=None):
        self._set_role("qa", fm)
        t0 = time.time()
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_codex=fake_codex, fake_claude=fake_claude),
                                       "HOME": self._fake_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(time.time() - t0, 20)          # the 60s sleep was cut short
        row = q(self.root, "SELECT status, log_path FROM runs ORDER BY id DESC LIMIT 1")
        self.assertEqual(row[0], "failed")
        self.assertIn("timed out", open(row[1]).read())
        return r

    def test_max_minutes_kills_a_claude_run_and_records_it(self):
        fake = 'echo \'{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\'\nsleep 60\n'
        self._timeout_run("max_minutes: 0.05\n", fake_claude=fake)

    def test_max_minutes_kills_a_codex_run_too(self):
        fake = 'echo \'{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"working"}}\'\nsleep 60\n'
        self._timeout_run("provider: openai\nmax_minutes: 0.05\n", fake_codex=fake)

    def test_unknown_plugin_or_mcp_name_is_named_in_the_console(self):
        argv = os.path.join(self.root, "claude-argv")
        self._set_role("qa", "plugins: ghost\nmcp: nope\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake_claude=self._fake_claude(argv)),
                                       "HOME": self._fake_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # the run still goes, without them
        self.assertIn("ghost", r.stdout + r.stderr)
        self.assertIn("nope", r.stdout + r.stderr)

    def _set_role(self, agent, fm):
        with open(os.path.join(self.root, "projects", "demo", "agents", agent + ".md"), "w") as f:
            f.write("---\n%s---\npersona\n" % fm)

    def test_openai_role_without_codex_cli_fails_before_recording_a_run(self):
        self._set_role("qa", "provider: openai\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin()})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("codex", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT COUNT(*) FROM runs")[0], 0)   # nothing recorded

    def test_anthropic_role_without_claude_cli_fails_before_recording_a_run(self):
        self._set_role("qa", "")
        r = self._run_agent("qa", env={"PATH": self._tmpbin()})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claude", r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT COUNT(*) FROM runs")[0], 0)

    def test_run_row_records_the_provider(self):
        fake = ('echo \'{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"ok"}}\'\n'
                'echo \'{"type":"turn.completed","usage":{}}\'\n')
        self._set_role("qa", "provider: openai\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT provider FROM runs ORDER BY id DESC LIMIT 1")[0], "openai")
        self._set_role("qa", "")
        r = self._run_agent("qa", env={"DAIS_NOOP_RUN": "true"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT provider FROM runs ORDER BY id DESC LIMIT 1")[0], "anthropic")

    def test_run_row_stores_the_usage_sidecar(self):
        fake = ('echo \'{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"ok"}}\'\n'
                'echo \'{"type":"turn.completed","usage":{"input_tokens":16276,"cached_input_tokens":11008,'
                '"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}\'\n')
        self._set_role("qa", "provider: openai\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = q(self.root, "SELECT input_tokens, cache_read_tokens, output_tokens, cost_usd, turns "
                           "FROM runs ORDER BY id DESC LIMIT 1")
        self.assertEqual(tuple(row), (16276, 11008, 5, None, 1))
        logs = os.listdir(os.path.join(self.root, "projects", "demo", "logs"))
        self.assertFalse(any(f.endswith(".usage.json") for f in logs))   # consumed, not littered

    def test_run_without_a_usage_report_stores_nulls(self):
        self._set_role("qa", "")
        r = self._run_agent("qa", env={"DAIS_NOOP_RUN": "true"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = q(self.root, "SELECT input_tokens, cost_usd, turns FROM runs ORDER BY id DESC LIMIT 1")
        self.assertEqual(tuple(row), (None, None, None))

    def test_openai_top_level_error_marks_run_failed(self):
        # codex exits 0 on an API error (e.g. a model the ChatGPT plan can't use); the run
        # must still land as 'failed', never as a 'succeeded' no-op the throttle then parks.
        fake = ('echo \'{"type":"thread.started","thread_id":"t"}\'\n'
                'echo \'{"type":"turn.started"}\'\n'
                'echo \'{"type":"error","message":"The x model is not supported"}\'\n'
                'exit 0\n')
        self._set_role("qa", "provider: openai\nmodel: x\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # run-agent itself completes
        self.assertEqual(q(self.root, "SELECT status FROM runs ORDER BY id DESC LIMIT 1")[0], "failed")

    def test_openai_adapter_passes_model_effort_and_ephemeral(self):
        argv = os.path.join(self.root, "codex-argv")
        fake = ('printf "%s\\n" "$@" > "' + argv + '"\n'
                # codex prints "Reading additional input from stdin..." and can block on an
                # inherited terminal; the adapter must hand it a closed stdin
                'if read -r _x; then echo stdin=open >> "' + argv + '"; else echo stdin=closed >> "' + argv + '"; fi\n'
                'echo \'{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"ok"}}\'\n'
                'echo \'{"type":"turn.completed","usage":{}}\'\n')
        self._set_role("qa", "provider: openai\nmodel: gpt-5.4\neffort: low\n")
        r = self._run_agent("qa", env={"PATH": self._tmpbin(fake)}, stdin_text="stray terminal input\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = open(argv).read().split("\n")
        self.assertIn("stdin=closed", args)
        self.assertIn("exec", args)
        self.assertIn("--json", args)
        self.assertIn("--ephemeral", args)            # no session piles up in ~/.codex per headless run
        self.assertEqual(args[args.index("-m") + 1], "gpt-5.4")
        self.assertIn("model_reasoning_effort=low", args)
        self.assertEqual(q(self.root, "SELECT status FROM runs ORDER BY id DESC LIMIT 1")[0], "succeeded")


class TestWorkspaceContextInjection(CliTest):
    """run-agent.sh injects the WORKSPACE CONTEXT.md (company-wide rules) ahead of
    the project's CONTEXT.md, so every agent run honors workspace-level decisions.
    Tested through the DAIS_SHOW_PROMPT=1 debug seam: it dumps the assembled prompt
    and exits before any claude call, so we assert wiring without a model run."""

    def _run_agent(self, repo_base, env_extra=None):
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": repo_base, "DAIS_SHOW_PROMPT": "1"})
        if env_extra:
            e.update(env_extra)
        return subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"),
                               "demo", "engineer"],
                              capture_output=True, text=True, env=e, cwd=self.root)

    def _scaffold_with_repo(self):
        # scaffold a project whose RELATIVE repo: resolves to an existing dir, so
        # run-agent.sh gets past its `[ -d "$REPO" ]` guard to assemble the prompt.
        dais(self.root, "scaffold", "demo")
        base = tempfile.mkdtemp(prefix="dais-repos-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        os.makedirs(os.path.join(base, "demo"))
        return base

    def test_workspace_context_injected_when_present(self):
        base = self._scaffold_with_repo()
        # `dais init` (CliTest.setUp) created the workspace CONTEXT.md at self.root
        self.assertTrue(os.path.exists(os.path.join(self.root, "CONTEXT.md")))
        r = self._run_agent(base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Workspace context:", r.stdout)
        self.assertIn(os.path.join(self.root, "CONTEXT.md"), r.stdout)
        # the project context line is still there, AFTER the workspace one
        self.assertIn("Project context", r.stdout)
        self.assertLess(r.stdout.index("Workspace context:"),
                        r.stdout.index("Project context"))

    # --- plan 3.1: the deterministic first turns are INLINED into the cached prompt prefix ----
    def test_context_bodies_are_inlined_not_pointed_at(self):
        # every run spent its first turns on `task show` + two Read calls for files the harness
        # already has in hand; inline them and the agent starts on the work
        base = self._scaffold_with_repo()
        with open(os.path.join(self.root, "CONTEXT.md"), "a") as f:
            f.write("\nWS-RULE-MARKER: ship on Fridays only\n")
        with open(os.path.join(self.root, "projects", "demo", "CONTEXT.md"), "a") as f:
            f.write("\nPROJECT-GOTCHA-MARKER: the test db needs port 5433\n")
        r = self._run_agent(base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WS-RULE-MARKER", r.stdout)
        self.assertIn("PROJECT-GOTCHA-MARKER", r.stdout)
        self.assertLess(r.stdout.index("WS-RULE-MARKER"), r.stdout.index("PROJECT-GOTCHA-MARKER"))
        self.assertNotIn("FIRST read", r.stdout)          # no round trip asked for what is already here

    def test_pinned_task_record_is_inlined(self):
        base = self._scaffold_with_repo()
        dais(self.root, "task", "add", "demo", "the work", "--id", "d-1", "--status", "ready",
             "--notes", "SPEC-MARKER: acceptance = the button turns green")
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": base, "DAIS_SHOW_PROMPT": "1", "DAIS_TASK_ID": "d-1"})
        r = subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", "engineer"],
                           capture_output=True, text=True, env=e, cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dispatched for task **d-1**", r.stdout)
        self.assertIn("SPEC-MARKER", r.stdout)             # the notes log, inline
        self.assertIn("status = ready", r.stdout)          # the record's fields, inline

    def test_oversized_context_is_capped_with_a_marker(self):
        base = self._scaffold_with_repo()
        with open(os.path.join(self.root, "projects", "demo", "CONTEXT.md"), "w") as f:
            f.write("HEAD-MARKER\n" + ("x" * 100 + "\n") * 400 + "TAIL-MARKER\n")   # ~40KB
        r = self._run_agent(base)
        self.assertIn("HEAD-MARKER", r.stdout)
        self.assertNotIn("TAIL-MARKER", r.stdout)
        self.assertIn("truncated", r.stdout)
        self.assertIn("24KB", r.stdout)

    def test_no_workspace_line_when_file_absent(self):
        base = self._scaffold_with_repo()
        os.remove(os.path.join(self.root, "CONTEXT.md"))   # drop the workspace context
        r = self._run_agent(base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Workspace context:", r.stdout)
        self.assertIn("Project context", r.stdout)         # project line unaffected


class TestMachinePromptInjection(CliTest):
    """The agent prompt is machine-native: the coordination block is ALWAYS injected (every
    project resolves a machine — its own machine.json, a `machine:` selector, or the coding
    default), derived from that machine (states + this role's own edges), with NO legacy
    closed-set status vocabulary. Asserted via the DAIS_SHOW_PROMPT seam."""

    def _prompt(self, agent="engineer"):
        dais(self.root, "scaffold", "demo")
        base = tempfile.mkdtemp(prefix="dais-repos-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        os.makedirs(os.path.join(base, "demo"))
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": base, "DAIS_SHOW_PROMPT": "1"})
        return subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"),
                               "demo", agent],
                              capture_output=True, text=True, env=e, cwd=self.root).stdout

    def test_machine_block_always_injected(self):
        # scaffolded projects have machine.json but NO `machine:` key in project.yaml —
        # the block must inject anyway (the old gate on `machine:` left agents blind).
        out = self._prompt()
        self.assertIn("dais fire", out)
        self.assertIn("dais edges", out)
        self.assertIn("proposal_review", out)              # the machine's state vocabulary
        self.assertIn("NEVER set a status directly", out)

    def test_roles_own_edges_are_listed(self):
        out = self._prompt("engineer")
        self.assertIn("claim", out)                         # ready --claim--> doing
        self.assertIn("complete", out)                      # doing --complete--> qa_review
        qa = self._prompt("qa")
        self.assertIn("fail", qa)                           # qa_review --fail--> blocked

    def test_no_legacy_status_vocabulary(self):
        out = self._prompt()
        for legacy in ("CLOSED set", "needs_qa", "ready_to_merge", "needs_review",
                       "changes_requested", "dais handoff", "dais backlog"):
            self.assertNotIn(legacy, out)


class TestRolePlaybook(CliTest):
    """Conventions are bound at the ROLE via a playbook (agents/<role>.md frontmatter → project
    default → built-in `code`), injected into the agent prompt. De-codes the harness for
    non-code domains while keeping coding conventions intact for code roles. Asserted via the
    DAIS_SHOW_PROMPT seam."""

    def _scaffold_with_repo(self):
        dais(self.root, "scaffold", "demo")
        base = tempfile.mkdtemp(prefix="dais-repos-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        os.makedirs(os.path.join(base, "demo"))
        return base

    def _prompt(self, base, agent="engineer"):
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": base, "DAIS_SHOW_PROMPT": "1"})
        return subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", agent],
                              capture_output=True, text=True, env=e, cwd=self.root).stdout

    def _pin_engineer_playbook(self, name):
        # inject a `playbook:` key into agents/engineer.md's frontmatter block (between its
        # two `---` markers) — the frontmatter-era equivalent of the old roles-column pin.
        p = os.path.join(self.root, "projects", "demo", "agents", "engineer.md")
        lines = open(p).read().splitlines()
        assert lines[0] == "---", "engineer.md has no frontmatter block"
        close = lines[1:].index("---") + 1
        lines.insert(close, "playbook: " + name)
        with open(p, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_code_role_keeps_coding_conventions(self):
        base = self._scaffold_with_repo()
        out = self._prompt(base)                     # engineer, no column -> project (none) -> code
        self.assertIn("Working conventions (code)", out)
        self.assertIn("Open PRs", out)
        self.assertIn("origin/main", out)
        self.assertIn("Coordination runs through", out)   # neutral contract intact

    def test_legal_playbook_swaps_out_coding(self):
        base = self._scaffold_with_repo()
        pb = os.path.join(self.root, "projects", "demo", "playbooks")
        os.makedirs(pb, exist_ok=True)
        with open(os.path.join(pb, "legal.md"), "w") as fh:
            fh.write("Cite authorities in Bluebook form. Nothing is filed without partner sign-off.\n")
        with open(os.path.join(self.root, "projects", "demo", "project.yaml"), "a") as fh:
            fh.write("playbook: legal\n")
        out = self._prompt(base)
        self.assertIn("Working conventions (legal)", out)
        self.assertIn("Bluebook", out)
        self.assertNotIn("Open PRs", out)            # no coding mechanics for a legal role
        self.assertNotIn("origin/main", out)

    def test_role_column_overrides_project_default(self):
        base = self._scaffold_with_repo()
        with open(os.path.join(self.root, "projects", "demo", "project.yaml"), "a") as fh:
            fh.write("playbook: legal\n")            # project default = legal …
        self._pin_engineer_playbook("code")          # … but the role pins code
        out = self._prompt(base)
        self.assertIn("Working conventions (code)", out)   # role wins
        self.assertIn("Open PRs", out)

    def test_lint_warns_on_unresolvable_playbook(self):
        dais(self.root, "scaffold", "demo")
        self._pin_engineer_playbook("ghostbook")     # no such playbook file anywhere
        r = dais(self.root, "lint", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # warning, not error
        self.assertIn("ghostbook", r.stdout + r.stderr)


class TestRoleNew(CliTest):
    """`dais role new` has Claude design a role (persona + routing row); the founder confirms, then
    lint guards. The model call is stubbed via DAIS_ROLE_GEN so the flow is deterministic offline."""

    PROP = ("name: paralegal\ntrigger: reactive\nprec: 4\nplaybook: legal\n"
            "model: \neffort: \n---\n"
            "# Paralegal — demo\nYou pull authorities and draft a research memo, then hand off.\n")

    def _gen_stub(self, body):
        p = os.path.join(self.root, "gen.sh")
        with open(p, "w") as fh:
            fh.write("#!/usr/bin/env bash\ncat <<'PROP'\n" + body + "\nPROP\n")
        os.chmod(p, 0o755)
        return p

    def test_writes_frontmatter_persona_no_roles_file(self):
        dais(self.root, "scaffold", "demo")
        r = dais(self.root, "role", "new", "demo", "--desc", "a paralegal", "--yes",
                 env={"DAIS_ROLE_GEN": self._gen_stub(self.PROP)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        persona = os.path.join(self.root, "projects", "demo", "agents", "paralegal.md")
        self.assertTrue(os.path.exists(persona))
        body = open(persona).read()
        self.assertTrue(body.startswith("---\n"))
        self.assertIn("trigger: reactive", body)
        self.assertIn("prec: 4", body)
        self.assertIn("playbook: legal", body)
        self.assertIn("research memo", body)
        self.assertFalse(os.path.exists(os.path.join(self.root, "projects", "demo", "roles")))

    def test_writes_provider_when_the_designer_picks_one(self):
        # the generator is told the provider choice (anthropic | openai) — a role it puts on
        # codex must land in frontmatter, or the choice silently evaporates to the default
        dais(self.root, "scaffold", "demo")
        prop = ("name: reviewer\ntrigger: reactive\nprec: 6\nplaybook: code\n"
                "provider: openai\nmodel: gpt-5.4\neffort: \n---\n# Reviewer\nbody\n")
        r = dais(self.root, "role", "new", "demo", "--desc", "x", "--yes",
                 env={"DAIS_ROLE_GEN": self._gen_stub(prop)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        body = open(os.path.join(self.root, "projects", "demo", "agents", "reviewer.md")).read()
        self.assertIn("provider: openai\n", body)
        self.assertIn("model: gpt-5.4\n", body)

    def test_generator_prompt_offers_the_provider_choice(self):
        # the stub echoes the prompt it was given (DAIS_ROLE_GEN gets it on stdin? no — via
        # argv-less claude -p; the harness passes nothing) so assert on the prompt text the
        # CLI builds instead: it must mention both providers
        src = open(os.path.join(self.root, "dais")).read()
        self.assertIn("provider: <anthropic|openai", src)

    def test_rejects_bad_name_writes_nothing(self):
        dais(self.root, "scaffold", "demo")
        bad = self._gen_stub("name: bad name!\ntrigger: reactive\nprec: 5\nplaybook: code\n"
                             "model: \neffort: \n---\n# x\nbody\n")
        r = dais(self.root, "role", "new", "demo", "--desc", "x", "--yes", env={"DAIS_ROLE_GEN": bad})
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "projects", "demo", "agents", "bad.md")))

    def test_preserves_cadence_trigger_with_colon(self):
        # 'trigger: every:24h' must survive — the value parser splits on the FIRST colon only
        dais(self.root, "scaffold", "demo")
        prop = ("name: metrics\ntrigger: every:24h\nprec: 7\nplaybook: code\n"
                "model: \neffort: \n---\n# Metrics\nbody\n")
        r = dais(self.root, "role", "new", "demo", "--desc", "x", "--yes",
                 env={"DAIS_ROLE_GEN": self._gen_stub(prop)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        body = open(os.path.join(self.root, "projects", "demo", "agents", "metrics.md")).read()
        self.assertIn("trigger: every:24h", body)

    def test_strips_spaces_in_optional_fields(self):
        # a model padding values with spaces must not corrupt the written frontmatter lines
        dais(self.root, "scaffold", "demo")
        prop = ("name: metricstwo\ntrigger: reactive\nprec:  8 \nplaybook: code \n"
                "model: claude-haiku-4-5\neffort:  low \n---\n# X\nbody\n")
        r = dais(self.root, "role", "new", "demo", "--desc", "x", "--yes",
                 env={"DAIS_ROLE_GEN": self._gen_stub(prop)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        body = open(os.path.join(self.root, "projects", "demo", "agents", "metricstwo.md")).read()
        self.assertIn("prec: 8\n", body)
        self.assertIn("effort: low\n", body)

    def test_refuses_to_clobber_existing_role(self):
        dais(self.root, "scaffold", "demo")             # ships agents/engineer.md
        dup = self._gen_stub("name: engineer\ntrigger: reactive\nprec: 5\nplaybook: code\n"
                             "model: \neffort: \n---\n# eng\nbody\n")
        r = dais(self.root, "role", "new", "demo", "--desc", "dup", "--yes", env={"DAIS_ROLE_GEN": dup})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stdout + r.stderr)


class TestWorkspaceContextBloatLint(CliTest):
    """`dais lint` (no project arg) warns — but does not error — when the workspace
    CONTEXT.md grows too large, since it is injected into every agent run."""

    def _write_context(self, nlines):
        with open(os.path.join(self.root, "CONTEXT.md"), "w") as fh:
            fh.write("\n".join("line %d" % i for i in range(nlines)) + "\n")

    def test_bloated_workspace_context_warns_but_passes(self):
        self._write_context(200)
        r = dais(self.root, "lint")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # warning, not error
        out = r.stdout + r.stderr
        self.assertIn("CONTEXT.md is 200 lines", out)
        self.assertIn("keep it tight", out)

    def test_short_workspace_context_no_warning(self):
        self._write_context(20)
        r = dais(self.root, "lint")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("keep it tight", r.stdout + r.stderr)


class TestSchedulePortable(CliTest):
    def test_linux_prints_cron_line(self):
        r = dais(self.root, "schedule", "install", "600", env={"DAIS_FORCE_OS": "Linux"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("crontab", r.stdout.lower())
        self.assertIn("dais tick", r.stdout)
        self.assertIn("600", r.stdout)

    def test_unknown_os_is_graceful(self):
        r = dais(self.root, "schedule", "install", env={"DAIS_FORCE_OS": "Plan9"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dais watch", r.stdout)  # points the user at the portable fallback


class TestScaffold(CliTest):
    def test_scaffold_creates_a_valid_project(self):
        r = dais(self.root, "scaffold", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        base = os.path.join(self.root, "projects", "demo")
        for rel in ("project.yaml", "agents/lead.md",
                    "agents/engineer.md", "agents/qa.md", "CONTEXT.md", "logs"):
            self.assertTrue(os.path.exists(os.path.join(base, rel)), rel)
        with open(os.path.join(base, "project.yaml")) as fh:
            y = fh.read()
        self.assertIn("project: demo", y)
        self.assertNotIn("__PROJECT__", y)

    def test_scaffold_has_no_roles_file_and_frontmatter_agents(self):
        r = dais(self.root, "scaffold", "fresh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pdir = os.path.join(self.root, "projects", "fresh")
        self.assertFalse(os.path.exists(os.path.join(pdir, "roles")))
        lead = open(os.path.join(pdir, "agents", "lead.md")).read()
        self.assertTrue(lead.startswith("---\n"))
        self.assertIn("trigger: every:24h", lead)

    def test_scaffold_refuses_existing(self):
        dais(self.root, "scaffold", "demo")
        r = dais(self.root, "scaffold", "demo")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("exists", (r.stdout + r.stderr).lower())

    def test_scaffold_substitutes_every_copied_file(self):
        # the sed pass used to cover only project.yaml + CONTEXT.md — agents/*.md personas
        # (which several templates address to "__PROJECT__") were left unsubstituted, reaching
        # an agent's prompt verbatim. Check EVERY template, every file, zero hits.
        templates = sorted(os.listdir(os.path.join(REPO, "harness", "templates")))
        self.assertTrue(templates)     # sanity: the glob actually found templates
        for tmpl in templates:
            proj = "t_" + tmpl
            r = dais(self.root, "scaffold", proj, "--template", tmpl)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            base = os.path.join(self.root, "projects", proj)
            hits = []
            for dirpath, _dirs, files in os.walk(base):
                for fn in files:
                    p = os.path.join(dirpath, fn)
                    with open(p, "rb") as fh:
                        if b"__PROJECT__" in fh.read():
                            hits.append(os.path.relpath(p, base))
            self.assertEqual(hits, [], "template %r left __PROJECT__ unsubstituted in: %s"
                             % (tmpl, hits))

    def test_scaffold_rejects_non_slug_names(self):
        # A name with `/`, a space, or a sed metachar would break the sed
        # substitution or create nested dirs — reject it as a slug violation.
        for bad in ("bad/name", "has space", "amp&er"):
            r = dais(self.root, "scaffold", bad)
            out = (r.stdout + r.stderr).lower()
            self.assertNotEqual(r.returncode, 0, "expected nonzero for %r: %s" % (bad, out))
            self.assertIn("slug", out, "expected slug error for %r: %s" % (bad, out))
        # and nothing got created under projects/
        self.assertEqual(os.listdir(os.path.join(self.root, "projects")), [])


class TestLintFullProject(CliTest):
    def test_scaffolded_project_lints_clean(self):
        dais(self.root, "scaffold", "demo")
        # give it the keys a real project needs (template repo/github are placeholders but present)
        r = dais(self.root, "lint", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_missing_project_yaml_is_error(self):
        d = os.path.join(self.root, "projects", "broken")
        os.makedirs(os.path.join(d, "agents"))
        with open(os.path.join(d, "roles"), "w") as fh:
            fh.write("engineer edit reactive ready 20\n")
        with open(os.path.join(d, "agents", "engineer.md"), "w") as fh:
            fh.write("# Engineer\n")
        r = dais(self.root, "lint", "broken")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("project.yaml", r.stdout + r.stderr)


class TestDaisHome(unittest.TestCase):
    """The DAIS_HOME seam: workspace DATA (board + projects/) is read/written
    under DAIS_HOME, while tool CODE keeps loading from DAIS_ROOT (the sandbox).
    DAIS_HOME defaults to DAIS_ROOT, so the monolith keeps working unchanged."""

    def setUp(self):
        # tool CODE dir; deliberately NOT inited so we can prove the board is
        # NOT created here when DAIS_HOME points elsewhere.
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp(prefix="dais-home-")  # workspace DATA dir
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_home_relocates_board_and_projects(self):
        env = {"DAIS_HOME": self.home}
        # scaffold writes the project under DAIS_HOME/projects, not the tool dir
        r = dais(self.root, "scaffold", "demo", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "projects", "demo")),
                        "project should live under DAIS_HOME")
        self.assertFalse(os.path.exists(os.path.join(self.root, "projects", "demo")),
                         "project must NOT be created under the tool dir")
        # a DB-touching command writes the board under DAIS_HOME, not the tool dir
        r = dais(self.root, "task", "add", "demo", "X", "--id", "h-1", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home, "dais.db")),
                        "board should live under DAIS_HOME")
        self.assertFalse(os.path.exists(os.path.join(self.root, "dais.db")),
                         "board must NOT be created under the tool dir")
        # the row really lives in the relocated board
        self.assertEqual(q(self.home, "SELECT title FROM tasks WHERE id='h-1'")[0], "X")

    def test_default_home_is_root(self):
        # no DAIS_HOME -> monolith default: data lives under the tool dir.
        r = dais(self.root, "scaffold", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "projects", "demo")))
        dais(self.root, "task", "add", "demo", "X", "--id", "r-1")
        self.assertTrue(os.path.exists(os.path.join(self.root, "dais.db")))
        self.assertEqual(q(self.root, "SELECT title FROM tasks WHERE id='r-1'")[0], "X")


class TestBinarySymlinkResolves(CliTest):
    """Install story: symlink the binary onto PATH (~/.local/bin/dais -> repo/dais)
    and run it from anywhere. DAIS_ROOT must resolve back to the REAL tool dir
    (through the symlink) so it finds harness/, not the symlink's own dir."""

    def test_runs_through_a_path_symlink(self):
        bindir = tempfile.mkdtemp(prefix="dais-bin-")
        self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)
        link = os.path.join(bindir, "dais")
        os.symlink(os.path.join(self.root, "dais"), link)
        e = dict(os.environ)
        e["NO_COLOR"] = "1"
        # run via the symlink, from the symlink's dir, with no DAIS_ROOT hint
        r = subprocess.run([link, "status"], capture_output=True, text=True,
                           env=e, cwd=bindir)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("No such file", out)   # would mean it looked for harness/ in bindir
        self.assertNotIn("Traceback", out)


class TestInitBootstrap(CliTest):
    """`dais init [path]` bootstraps a workspace skeleton at the TARGET path
    (dais.yaml + CONTEXT.md + projects/ + .gitignore + board), idempotently —
    it never clobbers files that already exist."""

    def _fresh_dir(self, prefix="dais-ws-"):
        d = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _neutral_home(self):
        # a HOME with no ~/.dais/config so resolution can't read the real one
        return self._fresh_dir("dais-HOME-")

    def test_init_creates_workspace_skeleton(self):
        T = self._fresh_dir()
        r = dais(self.root, "init", T, env={"HOME": self._neutral_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel in ("dais.yaml", "CONTEXT.md", "projects", ".gitignore", "dais.db"):
            self.assertTrue(os.path.exists(os.path.join(T, rel)),
                            "init should create %s" % rel)
        self.assertTrue(os.path.isdir(os.path.join(T, "projects")))
        with open(os.path.join(T, "dais.yaml")) as fh:
            y = fh.read()
        self.assertIn("workspace: %s" % os.path.basename(T), y)
        self.assertIn("agent_repos:", y)

    def test_init_is_idempotent(self):
        T = self._fresh_dir()
        keep = os.path.join(T, "CONTEXT.md")
        with open(keep, "w") as fh:
            fh.write("KEEP ME — do not clobber\n")
        r = dais(self.root, "init", T, env={"HOME": self._neutral_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(keep) as fh:
            self.assertIn("KEEP ME", fh.read())  # the [ ! -f ] guard preserved it

    def test_init_gitignores_env(self):
        # .env carries secrets (the auth:api key transport) — init must gitignore it,
        # both on a fresh workspace and one whose .gitignore predates this convention.
        T = self._fresh_dir()
        r = dais(self.root, "init", T, env={"HOME": self._neutral_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(T, ".gitignore")) as fh:
            self.assertIn(".env", fh.read())


class TestWorkspaceResolution(CliTest):
    """`dais` resolves which workspace it operates on from where you're STANDING:
    (1) DAIS_HOME env, (2) nearest ancestor of cwd with a marker (dais.yaml or
    dais.db), (3) under the tool tree -> self-contained, (4) ~/.dais/config,
    (5) the tool dir. Assertions are on which DB file actually got the row.
    Config-branch tests pass a CONTROLLED HOME so the real ~/.dais/config (which
    points at a real workspace on this machine) can never interfere."""

    def _tmp(self, prefix):
        d = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _neutral_home(self):
        return self._tmp("dais-HOME-")  # no ~/.dais/config

    def _home_with_config(self, home_target):
        h = self._tmp("dais-HOME-")
        os.makedirs(os.path.join(h, ".dais"))
        with open(os.path.join(h, ".dais", "config"), "w") as fh:
            fh.write("home=%s\n" % home_target)
        return h

    def _init_ws(self, prefix):
        # a real workspace via `dais init`; neutral HOME keeps even a pre-impl run isolated
        d = self._tmp(prefix)
        dais(self.root, "init", d, env={"HOME": self._neutral_home()})
        return d

    def _run(self, cwd, *args, env=None):
        # like the shared dais() helper, but with a caller-chosen cwd (you must stand
        # somewhere other than self.root to exercise resolution).
        e = dict(os.environ)
        e["NO_COLOR"] = "1"
        if env:
            e.update(env)
        return subprocess.run([os.path.join(self.root, "dais"), *args],
                              capture_output=True, text=True, env=e, cwd=cwd)

    def test_cwd_workspace_wins_over_config(self):
        A = self._init_ws("dais-A-")
        B = self._init_ws("dais-B-")
        home = self._home_with_config(B)        # config points elsewhere (B)
        # standing in A; "the workspace you're standing in" must beat the config
        r = self._run(A, "task", "add", "demo", "X", "--id", "wsa-1",
                      env={"HOME": home})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(A, "SELECT title FROM tasks WHERE id='wsa-1'")[0], "X")
        self.assertIsNone(q(B, "SELECT title FROM tasks WHERE id='wsa-1'"))

    def test_walkup_from_subdir(self):
        A = self._init_ws("dais-A-")
        sub = os.path.join(A, "projects")       # a subdir of A with no marker of its own
        os.makedirs(sub, exist_ok=True)
        # a board op from the subdir walks UP to A's marker (A/dais.yaml)
        r = self._run(sub, "task", "add", "demo", "X", "--id", "wu-1",
                      env={"HOME": self._neutral_home()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(A, "SELECT title FROM tasks WHERE id='wu-1'")[0], "X")

    def test_config_used_when_not_standing_in_a_workspace(self):
        B = self._init_ws("dais-B-")
        N = self._tmp("dais-N-")                # neutral dir: no marker, not under the tool tree
        home = self._home_with_config(B)
        r = self._run(N, "task", "add", "demo", "X", "--id", "cfg-1",
                      env={"HOME": home})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(B, "SELECT title FROM tasks WHERE id='cfg-1'")[0], "X")

    def test_env_overrides_everything(self):
        A = self._init_ws("dais-A-")
        B = self._init_ws("dais-B-")
        C = self._init_ws("dais-C-")
        home = self._home_with_config(B)
        # standing in A, config -> B, but explicit DAIS_HOME=C beats both
        r = self._run(A, "task", "add", "demo", "X", "--id", "env-1",
                      env={"HOME": home, "DAIS_HOME": C})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(C, "SELECT title FROM tasks WHERE id='env-1'")[0], "X")
        self.assertIsNone(q(A, "SELECT title FROM tasks WHERE id='env-1'"))
        self.assertIsNone(q(B, "SELECT title FROM tasks WHERE id='env-1'"))


class TestActionsVerb(CliTest):
    """`dais actions <id>` lists the founder actions for a task plus the exact
    command for each (it shells harness/actions.py's __main__ lister)."""

    def test_lists_edges_for_a_proposed_task(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "An initiative",
             "--id", "a-1", "--status", "proposed")
        r = dais(self.root, "actions", "a-1")            # alias for `dais edges`
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("submit", r.stdout)               # the lead's edge from proposed
        self.assertIn("proposal_review", r.stdout)      # its target state

    def test_unknown_task_errors(self):
        r = dais(self.root, "actions", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no task", r.stdout + r.stderr)


class TestStartVerb(CliTest):
    """`dais start <id>` runs the role the MACHINE dispatches for the task's state
    (machine.dispatch_role — the same resolution the scheduler uses). States with no
    dispatch role explain themselves by band and exit nonzero."""

    def test_founder_gate_waits_on_you(self):
        # proposal_review has only founder edges (band NEEDS YOU) — nothing to launch.
        dais(self.root, "task", "add", "demo", "Idea",
             "--id", "s-1", "--status", "proposal_review")
        r = dais(self.root, "start", "s-1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("waits on YOU", r.stdout + r.stderr)
        self.assertIn("dais edges s-1", r.stdout + r.stderr)

    def test_waiting_state_explains_the_system_event(self):
        # blocked has only a system `unblocked` edge (band WAITING) — no agent to launch.
        dais(self.root, "task", "add", "demo", "Stuck",
             "--id", "s-4", "--status", "blocked")
        r = dais(self.root, "start", "s-4")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("system event", r.stdout + r.stderr)

    def test_done_has_nothing_to_run(self):
        dais(self.root, "task", "add", "demo", "Shipped",
             "--id", "s-2", "--status", "done")
        r = dais(self.root, "start", "s-2")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing to run", r.stdout + r.stderr)

    def test_unknown_task_errors(self):
        r = dais(self.root, "start", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no such task", r.stdout + r.stderr)

    def test_ready_resolves_the_machine_dispatch_role(self):
        # ready dispatches the engineer (machine edge, not roles-file `handles`).
        # run-agent.sh fails fast (no real repo/claude), so we don't assert success —
        # only that the role resolved, proven by the "starting <proj>/<role>" line
        # printed BEFORE the exec.
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "Build it",
             "--id", "s-3", "--status", "ready")
        r = dais(self.root, "start", "s-3")
        self.assertIn("starting demo/engineer", r.stdout)

    def test_proposed_resolves_the_lead(self):
        # proposed dispatches the LEAD under the machine (legacy start refused it and
        # pointed at the removed `dais approve`).
        dais(self.root, "scaffold", "demo2")
        dais(self.root, "task", "add", "demo2", "Idea",
             "--id", "s-5", "--status", "proposed")
        r = dais(self.root, "start", "s-5")
        self.assertIn("starting demo2/lead", r.stdout)


class TestArchive(CliTest):
    """dais archive/unarchive: a project.yaml flag hides the project from the board and
    dispatch; nothing in the db is touched, and unarchive is a full restore."""

    def _yaml(self, name="demo"):
        with open(os.path.join(self.root, "projects", name, "project.yaml")) as fh:
            return fh.read()

    def test_archive_round_trip(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "old work", "--id", "d-1")
        r = dais(self.root, "archive", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("archived: true", self._yaml())
        # data survives; the board hides it but names it
        self.assertEqual(q(self.root, "SELECT COUNT(*) FROM tasks WHERE project='demo'")[0], 1)
        out = dais(self.root, "status").stdout
        self.assertNotIn("▌ demo", out)
        self.assertIn("archived: demo", out)
        r = dais(self.root, "unarchive", "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("archived", self._yaml())
        self.assertIn("▌ demo", dais(self.root, "status").stdout)

    def test_archive_is_idempotent(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "archive", "demo")
        r = dais(self.root, "archive", "demo")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._yaml().count("archived:"), 1)   # never stacks flag lines

    def test_archive_unknown_project_fails(self):
        r = dais(self.root, "archive", "ghost")
        self.assertNotEqual(r.returncode, 0)

    def test_tick_refuses_an_archived_project(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "w", "--id", "d-2", "--status", "ready")
        dais(self.root, "archive", "demo")
        r = dais(self.root, "tick", "demo", "--dry-run")
        self.assertIn("archived", r.stdout + r.stderr)
        self.assertNotIn("starting", r.stdout)

    def test_start_refuses_an_archived_project(self):
        dais(self.root, "scaffold", "demo")
        dais(self.root, "task", "add", "demo", "w", "--id", "d-3", "--status", "ready")
        dais(self.root, "archive", "demo")
        r = dais(self.root, "start", "d-3")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("archived", r.stdout + r.stderr)


class TestDispatchTaskPinning(CliTest):
    """run-agent.sh pins the dispatching task to the run: it names the task in the standing prompt
    and records it on runs.task_id. An explicit DAIS_TASK_ID (dais start <id>) wins; otherwise the
    reactive trigger for THIS role is re-derived and pinned only when it's actually this role's
    (role-guard). Exercised through the DAIS_SHOW_PROMPT seam, which sits AFTER the run INSERT."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        self.repo_base = tempfile.mkdtemp(prefix="dais-repos-")
        self.addCleanup(shutil.rmtree, self.repo_base, ignore_errors=True)
        os.makedirs(os.path.join(self.repo_base, "demo"))

    def _ins(self, tid, status):
        c = sqlite3.connect(os.path.join(self.root, "dais.db"))
        c.execute("INSERT INTO tasks(id,project,title,status) VALUES(?,?,?,?)", (tid, "demo", tid, status))
        c.commit(); c.close()

    def _show_prompt(self, agent, env=None):
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": self.repo_base, "DAIS_SHOW_PROMPT": "1"})
        if env:
            e.update(env)
        r = subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", agent],
                           capture_output=True, text=True, env=e, cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def _last_run_task(self):
        return q(self.root, "SELECT task_id FROM runs ORDER BY id DESC LIMIT 1")[0]

    def test_reactive_trigger_is_pinned_to_prompt_and_run(self):
        self._ins("demo-5", "ready")                          # ready -> engineer
        out = self._show_prompt("engineer")
        self.assertIn("demo-5", out)                          # the prompt names the dispatched task
        self.assertEqual(self._last_run_task(), "demo-5")     # and the run row records it

    def test_explicit_task_id_overrides_rederivation(self):
        # dais start <id>: the founder chose demo-9 even though demo-5 is the reactive top for engineer
        self._ins("demo-5", "ready")
        self._ins("demo-9", "ready")
        out = self._show_prompt("engineer", env={"DAIS_TASK_ID": "demo-9"})
        self.assertIn("demo-9", out)
        self.assertEqual(self._last_run_task(), "demo-9")

    def test_role_guard_pins_nothing_when_trigger_is_another_role(self):
        self._ins("demo-3", "qa_review")                      # qa_review -> qa, NOT engineer
        self._show_prompt("engineer")
        self.assertIsNone(self._last_run_task())


class TestWorktreeIsolation(CliTest):
    """isolation:worktree gives each run a private git worktree off fresh origin/<default-branch>.
    A read-only run's worktree is torn down; one left dirty (or with commits) is kept as recoverable
    task state; a non-git repo warns and runs in place. Exercised through run-agent.sh with the
    DAIS_NOOP_RUN seam (a shell command stands in for the model), so create->run->teardown all run."""

    def setUp(self):
        super().setUp()
        dais(self.root, "scaffold", "demo")
        self.repo_base = tempfile.mkdtemp(prefix="dais-wt-")
        self.addCleanup(shutil.rmtree, self.repo_base, ignore_errors=True)

    def _isolate(self, role="engineer"):
        p = os.path.join(self.root, "projects", "demo", "agents", role + ".md")
        with open(p) as f:
            body = f.read()
        with open(p, "w") as f:
            f.write("---\nisolation: worktree\n---\n" + body)

    def _git_repo(self):
        repo = os.path.join(self.repo_base, "demo")
        origin = os.path.join(self.repo_base, "demo-origin.git")
        def g(args, cwd=None):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
        g(["init", "--bare", "-b", "main", origin])
        g(["init", "-b", "main", repo])
        g(["config", "user.email", "t@example.com"], cwd=repo)
        g(["config", "user.name", "t"], cwd=repo)
        with open(os.path.join(repo, "README.md"), "w") as f:
            f.write("hi\n")
        g(["add", "-A"], cwd=repo)
        g(["commit", "-m", "init"], cwd=repo)
        g(["remote", "add", "origin", origin], cwd=repo)
        g(["push", "-u", "origin", "main"], cwd=repo)
        g(["remote", "set-head", "origin", "main"], cwd=repo)
        return repo

    def _run(self, noop, role="engineer"):
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": self.repo_base, "DAIS_NOOP_RUN": noop})
        return subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", role],
                              capture_output=True, text=True, env=e, cwd=self.root)

    def _run_worktrees(self, repo):
        wt = os.path.join(repo, ".worktrees")
        return [d for d in (os.listdir(wt) if os.path.isdir(wt) else []) if d.startswith("run-")]

    def test_run_executes_inside_a_private_worktree(self):
        self._isolate()
        self._git_repo()
        marker = os.path.join(self.repo_base, "where.txt")
        self._run("pwd > %s" % marker)
        with open(marker) as f:
            where = f.read().strip()
        self.assertIn("/.worktrees/run-", where)   # ran in a private worktree, not the shared repo

    def test_worktree_links_shared_dependency_dirs(self):
        # plan 3.5: a fresh worktree paid `bun install` every run; `worktree_link:` symlinks the
        # repo's installed deps in before worktree_setup runs
        self._isolate()
        repo = self._git_repo()
        os.makedirs(os.path.join(repo, "node_modules", "left-pad"))
        with open(os.path.join(self.root, "projects", "demo", "project.yaml"), "a") as f:
            f.write("worktree_link: node_modules, .venv\n")           # .venv absent: skipped quietly
        marker = os.path.join(self.repo_base, "link.txt")
        self._run("readlink node_modules > %s; ls .venv > /dev/null 2>&1 || echo no-venv >> %s" % (marker, marker))
        out = open(marker).read()
        self.assertIn(os.path.join(repo, "node_modules"), out)
        self.assertIn("no-venv", out)

    def test_read_only_run_tears_down_its_worktree(self):
        self._isolate()
        repo = self._git_repo()
        self._run("true")                          # touches nothing
        self.assertEqual(self._run_worktrees(repo), [])   # no leftover

    def test_dirty_run_keeps_its_worktree(self):
        self._isolate()
        repo = self._git_repo()
        self._run("echo work > left.txt")          # leaves an uncommitted file
        left = self._run_worktrees(repo)
        self.assertEqual(len(left), 1)             # kept as recoverable task state
        self.assertTrue(os.path.exists(os.path.join(repo, ".worktrees", left[0], "left.txt")))

    def test_worktree_setup_hook_runs_inside_the_worktree(self):
        self._isolate()
        self._git_repo()
        with open(os.path.join(self.root, "projects", "demo", "project.yaml"), "a") as f:
            f.write("worktree_setup: touch SETUP_MARKER\n")
        marker = os.path.join(self.repo_base, "hook.txt")
        # the noop (in WORKDIR) sees the file the setup hook created before the model would run
        self._run("test -f SETUP_MARKER && echo ran > %s" % marker)
        self.assertTrue(os.path.exists(marker), "worktree_setup hook did not run in the worktree")

    def test_non_git_repo_runs_in_place_and_warns(self):
        self._isolate()
        os.makedirs(os.path.join(self.repo_base, "demo"))   # a plain dir, NOT a git repo
        marker = os.path.join(self.repo_base, "where.txt")
        r = self._run("pwd > %s" % marker)
        with open(marker) as f:
            where = f.read().strip()
        self.assertNotIn("/.worktrees/", where)             # ran in place
        self.assertIn("not a git repo", r.stdout + r.stderr)

    def _add_worktree(self, repo, name, branch=None):
        args = ["git", "-C", repo, "worktree", "add"]
        args += (["-b", branch] if branch else ["--detach"])
        w = os.path.join(repo, ".worktrees", name)
        subprocess.run(args + [w, "origin/main"], check=True, capture_output=True)
        return w

    def _backdate(self, path, days=5):
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def _sweep(self, repo, days=2):
        subprocess.run(["bash", "-c", 'source "%s/harness/lib.sh"; worktree_prune_sweep "%s" %d'
                        % (self.root, repo, days)],
                       check=True, capture_output=True, env={**os.environ, "DAIS_HOME": self.root})

    def test_prune_sweep_removes_a_stale_clean_worktree(self):
        repo = self._git_repo()
        w = self._add_worktree(repo, "run-901")   # detached at origin/main, clean, no commits
        self._backdate(w)
        self._sweep(repo)
        self.assertFalse(os.path.isdir(w))         # crashed-run leftover reaped

    def test_prune_sweep_keeps_a_stale_worktree_with_unpushed_commits(self):
        repo = self._git_repo()
        w = self._add_worktree(repo, "run-902", branch="feat-902")
        with open(os.path.join(w, "x.txt"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", w, "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", w, "-c", "user.email=a@b", "-c", "user.name=a",
                        "commit", "-m", "wip"], check=True, capture_output=True)
        self._backdate(w)
        self._sweep(repo)
        self.assertTrue(os.path.isdir(w))          # unpushed work is never yanked

    def test_prune_sweep_leaves_a_fresh_worktree_alone(self):
        repo = self._git_repo()
        w = self._add_worktree(repo, "run-903")    # clean but NOT stale (just created)
        self._sweep(repo)
        self.assertTrue(os.path.isdir(w))          # only stale worktrees are reaped


if __name__ == "__main__":
    unittest.main()
