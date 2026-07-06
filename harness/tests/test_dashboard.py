import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import dashboard as d  # harness/dashboard.py
import machine as MC

HARNESS = os.path.join(os.path.dirname(__file__), "..")


SCHEMA = """
CREATE TABLE tasks(id TEXT, project TEXT, title TEXT, status TEXT, assignee TEXT,
  priority TEXT, pr_url TEXT, notes TEXT, updated_at TEXT);
CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT,
  status TEXT, summary TEXT, log_path TEXT, started_at TEXT, ended_at TEXT);
"""


def _seed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO tasks(id,project,title,status,priority,assignee,pr_url,notes) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [
            ("cou-5a", "acme", "setup offer", "ready_to_merge", "high", "qa",
             "https://x/pull/42", "gated"),
            ("cou-9", "acme", "thing", "ready_to_merge", "medium", "qa", None, None),
            ("cou-1", "acme", "done thing", "done", "low", None, None, None),
            ("cou-7", "acme", "ready thing", "ready", "high", "engineer", None, None),
        ])
    conn.executemany(
        "INSERT INTO runs(project,agent,status,summary,log_path,started_at,ended_at) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            ("acme", "qa", "succeeded", "cou-9→ready_to_merge", "/tmp/qa.log",
             "2026-06-26 20:38:00", "2026-06-26 20:41:00"),
            ("acme", "lead", "running", None, "/tmp/lead.log",
             "2026-06-26 20:41:00", None),
            ("acme", "qa", "capped", None, "/tmp/c.log",
             "2026-06-26 20:10:00", "2026-06-26 20:10:30"),
        ])
    conn.commit()
    return conn


class TestPrimitives(unittest.TestCase):
    def test_truncate_words_short_untouched(self):
        self.assertEqual(d.truncate_words("hello world", 20), "hello world")

    def test_truncate_words_breaks_on_word_boundary(self):
        self.assertEqual(d.truncate_words("pass virtual review and ship", 14), "pass virtual…")

    def test_truncate_words_single_long_word_hard_cut(self):
        self.assertEqual(d.truncate_words("antidisestablishment", 8), "antidis…")

    def test_collapse_ids_under_limit(self):
        self.assertEqual(d.collapse_ids(["a", "b"]), "a, b")

    def test_collapse_ids_over_limit(self):
        ids = [f"x{i}" for i in range(10)]
        self.assertEqual(d.collapse_ids(ids, limit=8),
                         "x0, x1, x2, x3, x4, x5, x6, x7 (+2 more)")

    def test_minutes_between_basic(self):
        self.assertEqual(
            d.minutes_between("2026-06-26 20:40:00", "2026-06-26 20:44:30"), 4)

    def test_minutes_between_missing_returns_none(self):
        self.assertIsNone(d.minutes_between("2026-06-26 20:40:00", None))


class TestDisplayWidth(unittest.TestCase):
    """Width-aware clipping is what prevents the curses 'bleed' artifacts:
    a wide glyph or stray escape must never overrun its pane into the next row."""

    def test_ascii_width_is_char_count(self):
        self.assertEqual(d.disp_width("hello"), 5)

    def test_wide_char_counts_two(self):
        self.assertEqual(d._char_cols("界"), 2)
        self.assertEqual(d.disp_width("a界b"), 4)

    def test_control_and_combining_are_zero_width(self):
        self.assertEqual(d._char_cols("\x1b"), 0)   # ESC — the escape-sequence menace
        self.assertEqual(d._char_cols("\t"), 0)
        self.assertEqual(d._char_cols("́"), 0)  # combining acute accent

    def test_ui_glyphs_are_single_width(self):
        # the chrome we actually draw (▶ · ↻ ● │ ─) must measure as 1 col each,
        # or headers/rows would be mis-clipped and leave residue.
        for g in "▶·↻●│─":
            self.assertEqual(d._char_cols(g), 1, g)

    def test_clip_never_exceeds_budget(self):
        self.assertEqual(d.clip_cols("hello world", 5), "hello")
        # a wide char straddling the boundary is dropped whole, not split:
        # "a界" = 3 cols, the next 界 needs 2 but only 1 col is left → stop at 3.
        self.assertEqual(d.clip_cols("a界界界", 4), "a界")
        self.assertEqual(d.disp_width(d.clip_cols("a界界界", 4)), 3)
        self.assertTrue(d.disp_width(d.clip_cols("界界界", 3)) <= 3)

    def test_vs16_is_zero_width_and_stripped(self):
        # VS16 (U+FE0F) forces emoji presentation: terminals paint 2 cells while
        # advancing 1, so ⚠️-style sequences overdraw whatever comes next. We count
        # it as 0 and drop it in clip_cols so the base char renders narrow+aligned.
        self.assertEqual(d._char_cols("\ufe0f"), 0)
        warn = "\u26a0\ufe0f"             # ⚠️ = narrow base + VS16
        self.assertEqual(d.disp_width(warn), 1)
        self.assertNotIn("\ufe0f", d.clip_cols(warn + " x", 80))
        self.assertEqual(d.clip_cols(warn + "abc", 2), "\u26a0a")
        # inherently-wide emoji (EAW=W, no VS16 needed) are untouched
        self.assertEqual(d.disp_width("✅"), 2)
        self.assertEqual(d.disp_width(d.pad_cols(warn, 10)), 10)

    def test_clip_neutralises_control_chars(self):
        out = d.clip_cols("a\x1b[31mb\tc", 80)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\t", out)
        self.assertEqual(len(out), len("a\x1b[31mb\tc"))  # replaced, not removed

    def test_clip_zero_or_negative_cols_is_empty(self):
        self.assertEqual(d.clip_cols("abc", 0), "")
        self.assertEqual(d.clip_cols("abc", -3), "")

    def test_pad_fills_exact_display_width(self):
        self.assertEqual(d.disp_width(d.pad_cols("hi", 10)), 10)
        self.assertEqual(d.disp_width(d.pad_cols("a界b", 10)), 10)  # wide-aware pad
        # over-long input is clipped to the budget, never padded past it
        self.assertEqual(d.disp_width(d.pad_cols("hello world", 5)), 5)


class TestLogWrapping(unittest.TestCase):
    """Live-log lines must wrap (not truncate) and never overrun the pane width."""

    def test_short_line_unchanged(self):
        self.assertEqual(d.wrap_cols("hello world", 80), ["hello world"])

    def test_long_line_wraps_into_multiple(self):
        out = d.wrap_cols("word " * 40, 30)
        self.assertGreater(len(out), 1)
        for ln in out:
            self.assertLessEqual(d.disp_width(ln), 30)

    def test_unbroken_token_hard_breaks(self):
        # a long path/URL with no spaces still wraps instead of being lost
        out = d.wrap_cols("/" + "a" * 200, 40)
        self.assertGreater(len(out), 1)
        for ln in out:
            self.assertLessEqual(d.disp_width(ln), 40)
        # no characters dropped on a hard break
        self.assertEqual("".join(p.lstrip() for p in out), "/" + "a" * 200)

    def test_continuation_indent_applied(self):
        out = d.wrap_cols("alpha beta gamma delta epsilon zeta", 12,
                          subsequent_indent="  ")
        self.assertGreater(len(out), 1)
        self.assertTrue(all(ln.startswith("  ") for ln in out[1:]))

    def test_wide_chars_never_overrun(self):
        out = d.wrap_cols("界" * 50, 20)
        for ln in out:
            self.assertLessEqual(d.disp_width(ln), 20)


class TestLogColor(unittest.TestCase):
    """Each fmt-stream marker maps to a distinct colour; failures go red."""

    class _Fake:           # stand-in App: _cp echoes the pair id so we can assert it
        has_color = True
        def _cp(self, n):
            return n

    def attr(self, line):
        return d.App._log_attr(self._Fake(), line)

    def test_assistant_is_cyan(self):
        self.assertEqual(self.attr("  💬 reading the spec"), 3)

    def test_tool_call_is_yellow(self):
        self.assertEqual(self.attr("  🔧 Bash cd /repo && ls"), 4)

    def test_done_is_green_bold(self):
        import dashboard
        self.assertEqual(self.attr("  ✓ success 12s"), 1 | dashboard.curses.A_BOLD)

    def test_tool_output_is_dim(self):
        import dashboard
        self.assertEqual(self.attr("     ↳ 340 pass, 0 fail"),
                         6 | dashboard.curses.A_DIM)

    def test_failed_output_is_red(self):
        # a failing command's output overrides the dim default
        self.assertEqual(self.attr("     ↳ Exit code 128 fatal: needs a revision"), 2)
        self.assertEqual(self.attr("     ↳ Traceback (most recent call last)"), 2)

    def test_plain_line_uncoloured(self):
        self.assertEqual(self.attr("  some raw passthrough line"), 0)


class TestFmtStreamProvider(unittest.TestCase):
    """fmt-stream.py --provider openai maps codex `exec --json` JSONL onto the
    same markers the claude stream-json path produces."""

    def test_fmt_stream_openai_maps_markers(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "codex-exec.jsonl")
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        with open(fixture) as fin:
            subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"),
                            logpath, "--provider", "openai"],
                           stdin=fin, capture_output=True, text=True)
        log = open(logpath).read()
        self.assertIn("💬", log)                    # the agent_message mapped
        self.assertIn("✓", log)                     # turn completion mapped

    def test_fmt_stream_default_is_anthropic_unchanged(self):
        # a claude stream-json line still maps (regression: the provider arg is additive)
        line = json.dumps({"type": "assistant",
                            "message": {"content": [{"type": "text", "text": "hi"}]}})
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"), logpath],
                       input=line + "\n", capture_output=True, text=True)
        log = open(logpath).read()
        self.assertIn("💬 hi", log)


class TestDataLayer(unittest.TestCase):
    def test_running_agents_live_only(self):
        with tempfile.TemporaryDirectory() as dirp:
            open(os.path.join(dirp, ".lock-qa"), "w").write("111\n")
            open(os.path.join(dirp, ".lock-lead"), "w").write("222\n")
            alive = {111}
            self.assertEqual(
                d.running_agents(dirp, is_alive=lambda p: p in alive), ["qa"])

    def test_snapshot_groups_tasks_by_status(self):
        conn = _seed()
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        proj = {p.name: p for p in snap.projects}["acme"]
        self.assertEqual([t.id for t in proj.tasks_by_status["ready_to_merge"]],
                         ["cou-5a", "cou-9"])
        self.assertEqual(len(proj.tasks_by_status["done"]), 1)

    def test_snapshot_cap_state_within_90min(self):
        conn = _seed()
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertTrue(snap.cap_state)
        snap2 = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 23:59:00")
        self.assertFalse(snap2.cap_state)

    def test_snapshot_run_duration(self):
        conn = _seed()
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        done = [r for r in snap.recent_runs if r.status == "succeeded"][0]
        self.assertEqual(done.dur_min, 3)

    def test_snapshot_sees_project_without_roles_file(self):
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "newstyle")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "project.yaml"), "w") as f:
                f.write("project: newstyle\nrepo: x\nstage_goal: g\n")
            snap = d.load_snapshot(_seed(), root=root)
            self.assertIn("newstyle", [p.name for p in snap.projects])


class TestWorkspaceName(unittest.TestCase):
    """workspace_name reads the `workspace:` value from a workspace's dais.yaml
    (the line-based reader mirrors stage_goal), or None when absent."""

    def _ws(self, contents):
        root = tempfile.mkdtemp(prefix="dais-ws-")
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        if contents is not None:
            with open(os.path.join(root, "dais.yaml"), "w") as fh:
                fh.write(contents)
        return root

    def test_reads_workspace_value(self):
        root = self._ws("workspace: acme\nagent_repos: /work\n")
        self.assertEqual(d.workspace_name(root), "acme")

    def test_missing_yaml_is_none(self):
        root = self._ws(None)                       # no dais.yaml at all
        self.assertIsNone(d.workspace_name(root))

    def test_missing_key_is_none(self):
        root = self._ws("agent_repos: /work\n")     # yaml present, no workspace: key
        self.assertIsNone(d.workspace_name(root))

    def test_empty_value_is_none(self):
        root = self._ws("workspace:\n")
        self.assertIsNone(d.workspace_name(root))


class TestWorkspaceHeader(unittest.TestCase):
    """The plain header shows the workspace identity when present, else falls back
    to the generic STATUS banner; the workspace flows in via Snapshot.workspace."""

    def _snap(self, workspace):
        return d.Snapshot(projects=[], recent_runs=[], cap_state=False,
                          ts="2026-06-26 20:45:00", workspace=workspace)

    def test_header_shows_workspace_name(self):
        text = d.render_plain(self._snap("acme"), color=False)
        self.assertIn("DAIS · acme", text)
        self.assertNotIn("DAIS · STATUS", text)

    def test_header_falls_back_to_status(self):
        text = d.render_plain(self._snap(None), color=False)
        self.assertIn("DAIS · STATUS", text)

    def test_load_snapshot_populates_workspace_from_yaml(self):
        root = tempfile.mkdtemp(prefix="dais-ws-")
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "dais.yaml"), "w") as fh:
            fh.write("workspace: demo\n")
        snap = d.load_snapshot(_seed(), root=root, now="2026-06-26 20:45:00")
        self.assertEqual(snap.workspace, "demo")
        self.assertIn("DAIS · demo", d.render_plain(snap, color=False))


class TestRenderPlain(unittest.TestCase):
    def test_plain_no_color_and_collapses(self):
        conn = _seed()
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        text = d.render_plain(snap, color=False)
        self.assertNotIn("\033[", text)                 # no ANSI when color off
        self.assertIn("DAIS · STATUS", text)
        self.assertIn("ready", text)                    # `ready` is a machine phase
        self.assertIn("cou-5a", text)                   # shown under its (undeclared) phase line
        self.assertIn("done: 1", text)

    def test_no_hardcoded_owner_path(self):
        conn = _seed()
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        text = d.render_plain(snap, color=False)
        self.assertNotIn("Desktop/cedar", text)

    def test_plain_truncates_goal_on_word_boundary(self):
        long_goal = ("pass virtual review and ship the thing now to the lawyers "
                     "then iterate on the next milestone quickly afterward")
        self.assertGreater(len(long_goal), 84)  # must exceed the truncation width
        snap = d.Snapshot(
            projects=[d.Project(
                name="p", stage_goal=long_goal,
                running=[], tasks_by_status={}, recent_runs=[])],
            recent_runs=[], cap_state=False, ts="2026-06-26 20:45:00")
        text = d.render_plain(snap, color=False)
        self.assertIn("…", text)
        self.assertNotIn(long_goal, text)            # full goal not shown
        self.assertNotIn("milestone", text)          # tail was cut
        self.assertIn("pass virtual", text)          # head retained


class TestTuiSupport(unittest.TestCase):
    def test_needs_review_renders_as_founder_gate(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO tasks(id,project,title,status,priority,assignee) "
            "VALUES('lyr-19','beacon','Growth review','proposal_review','high','founder')")
        conn.commit()
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        text = d.render_plain(snap, color=False)
        self.assertIn("proposal review", text)          # a founder-gate phase
        self.assertIn("◆", text)                        # flagged as needs-you
        self.assertIn("lyr-19", text)

    def test_runs_touching_matches_summary(self):
        runs = [d.Run("2026-06-26 20:38:00", "qa", "succeeded",
                      summary="cou-9→ready_to_merge"),
                d.Run("2026-06-26 20:20:00", "eng", "succeeded",
                      summary="cou-5a→needs_qa")]
        self.assertEqual(len(d.runs_touching(runs, "cou-5a")), 1)

    def test_filter_rows(self):
        rows = ["acme", "echo", "beacon"]
        self.assertEqual(d.filter_rows(rows, "ECH", key=lambda r: r),
                         ["echo"])

    def test_short_summary_collapses_multiple(self):
        self.assertEqual(d.short_summary("a→x, b→y, c→z"), "a → x  (+2 more)")

    def test_short_summary_spaces_arrows(self):
        self.assertEqual(d.short_summary("only→one"), "only → one")

    def test_short_summary_empty(self):
        self.assertEqual(d.short_summary(None), "")


class TestRunningVisibility(unittest.TestCase):
    def test_seconds_between_and_fmt(self):
        self.assertEqual(d.seconds_between("2026-06-26 20:40:00",
                                           "2026-06-26 20:40:45"), 45)
        self.assertEqual(d.fmt_elapsed(45), "45s")
        self.assertEqual(d.fmt_elapsed(123), "2:03")
        self.assertEqual(d.fmt_elapsed(3720), "1h02m")
        self.assertEqual(d.fmt_elapsed(None), "")

    def test_elapsed_positive_across_utc_now(self):
        # regression for the "always 0m" bug: started (UTC) vs utc_now() must be >= 0
        # and grow, not go negative (which clamped to 0).
        past = "2000-01-01 00:00:00"
        self.assertGreater(d.seconds_between(past, d.utc_now()), 0)

    def test_to_local_hhmm_format(self):
        self.assertRegex(d.to_local_hhmm("2026-06-26 20:40:00"), r"^\d\d:\d\d$")
        self.assertRegex(d.to_local_hhmm("2026-06-26 20:40:00", with_secs=True),
                         r"^\d\d:\d\d:\d\d$")
        self.assertEqual(d.to_local_hhmm(None), "--:--")

    def test_running_task_id_guesses_a_dispatch_state(self):
        m = MC.load(MC.default_machine_path())
        p = d.Project(name="p", stage_goal="", machine=m,
                      tasks_by_status={"ready": [d.Task("p-1", "x", "ready", "high")]})
        self.assertEqual(d.running_task_id(p), "p-1")   # ready is a dispatch (QUEUED) state
        p2 = d.Project(name="p", stage_goal="", machine=m,
                       tasks_by_status={"approved": [d.Task("p-2", "y", "approved", "high")]})
        self.assertEqual(d.running_task_id(p2), "")     # approved parks -> no guess

    def test_running_threads_collects_all_agents(self):
        snap = d.Snapshot(
            projects=[
                d.Project(name="beacon", stage_goal="",
                          running=[("engineer", "2026-06-26 20:40:00")],
                          machine=MC.load(MC.default_machine_path()),
                          tasks_by_status={"doing": [d.Task("lyr-1", "t", "doing", "high")]},
                          recent_runs=[d.Run("2026-06-26 20:40:00", "engineer", "running",
                                             log_path="/tmp/x.log")]),
                d.Project(name="wb", stage_goal="",
                          running=[("qa", "2026-06-26 20:44:00")], tasks_by_status={}),
            ],
            recent_runs=[], cap_state=False, ts="2026-06-26 20:45:00")
        threads = d.running_threads(snap, now="2026-06-26 20:45:00")
        self.assertEqual(len(threads), 2)
        eng = [t for t in threads if t["agent"] == "engineer"][0]
        self.assertEqual(eng["task"], "lyr-1")
        self.assertEqual(eng["secs"], 300)
        self.assertEqual(eng["log_path"], "/tmp/x.log")

    def test_running_thread_log_survives_a_newer_finished_run(self):
        # engineer started first and is STILL running; qa ran after it and finished, so the
        # project's newest run isn't 'running'. The engineer thread must still tail ITS OWN log
        # (found by agent), not show '(waiting for output…)' because recent_runs[0] finished.
        snap = d.Snapshot(
            projects=[d.Project(name="wb", stage_goal="",
                                running=[("engineer", "2026-07-01 16:24:00")],
                                machine=MC.load(MC.default_machine_path()),
                                tasks_by_status={},
                                recent_runs=[
                                    d.Run("2026-07-01 16:25:00", "qa", "succeeded",
                                          log_path="/tmp/qa.log"),
                                    d.Run("2026-07-01 16:24:00", "engineer", "running",
                                          log_path="/tmp/eng.log"),
                                ])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        threads = d.running_threads(snap, now="2026-07-01 16:30:00")
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["log_path"], "/tmp/eng.log")

    def test_concurrent_threads_each_get_their_own_log(self):
        snap = d.Snapshot(
            projects=[d.Project(name="wb", stage_goal="",
                                running=[("engineer", "2026-07-01 16:24:00"),
                                         ("qa", "2026-07-01 16:25:00")],
                                machine=MC.load(MC.default_machine_path()),
                                tasks_by_status={},
                                recent_runs=[
                                    d.Run("2026-07-01 16:25:00", "qa", "running",
                                          log_path="/tmp/qa.log"),
                                    d.Run("2026-07-01 16:24:00", "engineer", "running",
                                          log_path="/tmp/eng.log"),
                                ])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        logs = {t["agent"]: t["log_path"]
                for t in d.running_threads(snap, now="2026-07-01 16:30:00")}
        self.assertEqual(logs, {"engineer": "/tmp/eng.log", "qa": "/tmp/qa.log"})

    def test_tail_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("line one\nline two\n\nline three\n")
            path = fh.name
        try:
            self.assertEqual(d.tail_lines(path, 2), ["", "line three"])
            self.assertEqual(d.tail_lines("/no/such/file"), [])
        finally:
            os.unlink(path)

    def test_find_task(self):
        snap = d.Snapshot(projects=[d.Project(name="p", stage_goal="",
            tasks_by_status={"doing": [d.Task("p-1", "title one", "doing", "high")]})],
            recent_runs=[], cap_state=False, ts="2026-06-26 20:45:00")
        self.assertEqual(d.find_task(snap, "p", "p-1").title, "title one")
        self.assertIsNone(d.find_task(snap, "p", "nope"))
        self.assertIsNone(d.find_task(snap, "p", ""))

    def test_project_roles_reads_file(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects", "p"))
            with open(os.path.join(root, "projects", "p", "roles"), "w") as fh:
                fh.write("# comment\nqa review reactive needs_qa 1\n"
                         "engineer edit reactive ready 2\n")
            self.assertEqual(d.project_roles(root, "p"), ["qa", "engineer"])

    def test_project_roles_from_agents_dir(self):
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "p")
            os.makedirs(os.path.join(pdir, "agents"))
            with open(os.path.join(pdir, "project.yaml"), "w") as f:
                f.write("project: p\nrepo: x\nstage_goal: g\n")
            with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
                f.write("---\ntrigger: reactive\n---\npersona\n")
            roles = d.project_roles(root, "p")
            self.assertIn("qa", roles)


class TestControl(unittest.TestCase):
    def test_parse_pr_from_url(self):
        self.assertEqual(d.parse_pr("https://github.com/x/y/pull/42"), "42")

    def test_parse_pr_none_or_bad(self):
        self.assertEqual(d.parse_pr(None), "")
        self.assertEqual(d.parse_pr("not a url"), "")

    def test_watch_state_stopped(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            self.assertEqual(d.watch_state(root)[0], "stopped")

    def test_watch_state_running_reads_interval_par(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 3")
            state, interval, par = d.watch_state(root)
            self.assertEqual(state, "running")
            self.assertEqual(interval, "900")
            self.assertEqual(par, "3")

    def test_watch_state_tolerates_next_tick_field(self):
        # the loop stamps a 4th field (next-tick epoch) each cycle — the 3-tuple parse must not care
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 3 1751844000")
            state, interval, par = d.watch_state(root)
            self.assertEqual((state, interval, par), ("running", "900", "3"))

    def test_watch_next_tick_reads_fourth_field(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 3 1751844000")
            self.assertEqual(d.watch_next_tick(root), 1751844000)

    def test_watch_next_tick_none_pre_countdown_or_absent(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            self.assertIsNone(d.watch_next_tick(root))            # no pidfile
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 3")                  # old 3-field shape
            self.assertIsNone(d.watch_next_tick(root))

    def test_fmt_countdown(self):
        self.assertEqual(d.fmt_countdown(-5), "due")
        self.assertEqual(d.fmt_countdown(0), "due")
        self.assertEqual(d.fmt_countdown(42), "42s")
        self.assertEqual(d.fmt_countdown(60), "1:00")
        self.assertEqual(d.fmt_countdown(893), "14:53")

    def test_fmt_age(self):
        self.assertEqual(d.fmt_age("2026-07-06 10:00:00", "2026-07-06 10:00:30"), "")   # <1m: quiet
        self.assertEqual(d.fmt_age("2026-07-06 10:00:00", "2026-07-06 10:45:00"), "45m")
        self.assertEqual(d.fmt_age("2026-07-06 10:00:00", "2026-07-06 17:00:00"), "7h")
        self.assertEqual(d.fmt_age("2026-07-05 10:00:00", "2026-07-06 17:00:00"), "31h")  # <48h stays hours
        self.assertEqual(d.fmt_age("2026-07-03 10:00:00", "2026-07-06 17:00:00"), "3d")
        self.assertEqual(d.fmt_age(None, "2026-07-06 17:00:00"), "")                     # unparseable: quiet

    def test_fmt_model(self):
        self.assertEqual(d.fmt_model("claude-fable-5"), "fable-5")
        self.assertEqual(d.fmt_model("claude-opus-4-8[1m]"), "opus-4-8[1m]")
        self.assertEqual(d.fmt_model("gpt-5.1-codex-mini"), "gpt-5.1-codex-mini")
        self.assertEqual(d.fmt_model(None), "")

    def test_watch_state_paused_wins(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 1")
            open(os.path.join(root, "projects", ".paused"), "w").close()
            self.assertEqual(d.watch_state(root)[0], "paused")

    def test_watch_state_dead_pid_is_stopped(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write("999999 900 1")          # almost certainly not a live pid
            self.assertEqual(d.watch_state(root)[0], "stopped")


class TestActMachineConditionalAttest(unittest.TestCase):
    """The panel's greenlight prompt must agree with the engine on a CONDITIONAL attest
    (`attest:<fact> when task:<flag>`): an explicitly-false flag lifts the prompt, while
    NULL/unknown still prompts (fail-safe) — the panel elicits exactly what fire() will demand."""

    class _FakeApp:
        """Stand-in App: real _act_machine logic, stubbed I/O. Records every prompt and the
        argv it would dispatch."""
        _act_machine = d.App._act_machine

        def __init__(self, conn):
            self.conn = conn
            self.flash = None
            self.prompts = []
            self.dispatched = None

        def _prompt(self, label):
            self.prompts.append(label)
            if "type the task id" in label:
                return "rel-1"
            if "attest" in label:                     # answer the attest prompt honestly
                return label.split("'")[1]
            return ""

        def _confirm(self, msg):
            return True

        def _dispatch_out(self, cmd):
            self.dispatched = cmd
            return 0, ""

        def refresh(self):
            pass

    def _app(self, touches_migrations):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks(id TEXT, project TEXT, status TEXT, title TEXT,"
                     " assignee TEXT, parked_from TEXT, touches_migrations INTEGER)")
        conn.execute("INSERT INTO tasks(id,project,status,title,touches_migrations)"
                     " VALUES('rel-1','acme','release_review','Release: acme',?)",
                     (touches_migrations,))
        return self._FakeApp(conn)

    _MACHINE = {"edges": [{"from": "release_review", "to": "releasing", "by": "founder",
                           "verb": "greenlight",
                           "guards": ["typed_confirm",
                                      "attest:migrations_applied when task:touches_migrations"]}]}
    _TASK = {"id": "rel-1", "status": "release_review"}

    def test_false_flag_skips_the_attest_prompt(self):
        app = self._app(touches_migrations=0)
        app._act_machine(self._MACHINE, "greenlight", None, self._TASK)
        self.assertEqual(len(app.prompts), 1)                       # typed_confirm only
        self.assertIn("type the task id", app.prompts[0])
        self.assertIsNotNone(app.dispatched)                        # it DID fire
        self.assertNotIn("--attest", app.dispatched)

    def test_null_flag_still_prompts(self):
        app = self._app(touches_migrations=None)
        app._act_machine(self._MACHINE, "greenlight", None, self._TASK)
        self.assertTrue(any("attest" in p for p in app.prompts))
        self.assertIn("--attest", app.dispatched)
        self.assertIn("migrations_applied", app.dispatched)


class TestRenderProjectCast(unittest.TestCase):
    """`dais project <name>`'s cast now renders straight from router.cast()/agent_setup() —
    the agents/ directory IS the cast, no roles-file read, no active_agents gating."""

    def test_render_project_cast_without_roles_file(self):
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "p")
            os.makedirs(os.path.join(pdir, "agents"))
            with open(os.path.join(pdir, "project.yaml"), "w") as f:
                f.write("project: p\nrepo: x\nstage_goal: g\nmodel: claude-opus-4-8\n")
            with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
                f.write("---\nmodel: claude-haiku-4-5\n---\npersona\n")
            out = d.render_project(root, "p", color=False)
            self.assertIn("qa", out)
            self.assertIn("claude-haiku-4-5", out)


if __name__ == "__main__":
    unittest.main()
