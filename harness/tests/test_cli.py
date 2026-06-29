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


class TestApproveAssignee(CliTest):
    def _project_with_builder(self, name, builder_role):
        d = os.path.join(self.root, "projects", name)
        os.makedirs(os.path.join(d, "agents"))
        with open(os.path.join(d, "roles"), "w") as fh:
            fh.write("# name access trigger handles prec\n")
            fh.write("%s edit reactive ready,changes_requested 20\n" % builder_role)

    def test_assignee_resolved_from_roles(self):
        self._project_with_builder("demo", "builder")
        dais(self.root, "task", "add", "demo", "Idea", "--id", "d-1", "--status", "proposed")
        r = dais(self.root, "approve", "d-1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q(self.root, "SELECT assignee FROM tasks WHERE id='d-1'")[0], "builder")
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-1'")[0], "ready")

    def test_assignee_falls_back_to_engineer(self):
        dais(self.root, "task", "add", "solo", "Idea", "--id", "s-1", "--status", "proposed")
        dais(self.root, "approve", "s-1")
        self.assertEqual(q(self.root, "SELECT assignee FROM tasks WHERE id='s-1'")[0], "engineer")


class TestRepoPath(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "projects", "demo"))

    def _repo_path(self, repo_value, env=None):
        yaml = os.path.join(self.root, "projects", "demo", "project.yaml")
        with open(yaml, "w") as fh:
            fh.write("project: demo\nrepo: %s\n" % repo_value)
        e = {"DAIS_ROOT": self.root, "PATH": os.environ["PATH"], "HOME": "/home/x"}
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

    def test_relative_default_base_is_parent_of_root(self):
        # default base = parent of DAIS_ROOT
        expected = os.path.join(os.path.dirname(self.root), "demo")
        self.assertEqual(self._repo_path("demo"), expected)


class TestMigrationsGlob(unittest.TestCase):
    """migrations_re turns a project's migrations_glob into a grep -E regex used by
    `dais ship` to detect DB migrations in a PR diff. The default must match BOTH
    supabase/migrations/*.sql and db/migrations/*.sql (the old guard was supabase-only)."""

    def setUp(self):
        self.root = make_sandbox()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "projects", "demo"))

    def _migrations_re(self, glob_value=None):
        yaml = os.path.join(self.root, "projects", "demo", "project.yaml")
        with open(yaml, "w") as fh:
            fh.write("project: demo\n")
            if glob_value is not None:
                fh.write("migrations_glob: %s\n" % glob_value)
        r = subprocess.run(["bash", "-c",
                            'source "%s/harness/lib.sh"; migrations_re demo' % self.root],
                           capture_output=True, text=True,
                           env={"DAIS_ROOT": self.root, "PATH": os.environ["PATH"]})
        return r.stdout.strip()

    def test_default_glob_matches_supabase_and_db(self):
        self.assertEqual(self._migrations_re(), r".*/migrations/.*\.sql$")

    def test_custom_glob_is_translated(self):
        self.assertEqual(self._migrations_re("drizzle/*.sql"), r"drizzle/.*\.sql$")


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
        for rel in ("project.yaml", "roles", "agents/lead.md",
                    "agents/engineer.md", "agents/qa.md", "CONTEXT.md", "logs"):
            self.assertTrue(os.path.exists(os.path.join(base, rel)), rel)
        with open(os.path.join(base, "project.yaml")) as fh:
            y = fh.read()
        self.assertIn("project: demo", y)
        self.assertNotIn("__PROJECT__", y)

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


if __name__ == "__main__":
    unittest.main()
