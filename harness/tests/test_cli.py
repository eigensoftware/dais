import os
import shutil
import sqlite3
import subprocess
import tempfile
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


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        dais(self.root, "init")


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

    def _run_agent(self, agent, env=None):
        # like _show_config, but WITHOUT DAIS_SHOW_CONFIG — the run must reach the
        # auth:api preflight (which sits after the config seam), not stop at it.
        e = dict(os.environ)
        e.update({"NO_COLOR": "1", "DAIS_ROOT": self.root, "DAIS_HOME": self.root,
                  "DAIS_AGENT_REPOS": self.repo_base})
        if env:
            e.update(env)
        return subprocess.run([os.path.join(self.root, "harness", "run-agent.sh"), "demo", agent],
                              capture_output=True, text=True, env=e, cwd=self.root)

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


if __name__ == "__main__":
    unittest.main()
