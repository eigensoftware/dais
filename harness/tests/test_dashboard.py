import json
import os
import shutil
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
  priority TEXT, pr_url TEXT, notes TEXT, updated_at TEXT, budget_lifted_at TEXT,
  state_entered_at TEXT);
CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT,
  task_id TEXT, status TEXT, summary TEXT, log_path TEXT, started_at TEXT, ended_at TEXT,
  provider TEXT, input_tokens INTEGER, cost_usd REAL);
CREATE TABLE run_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_id TEXT,
  verb TEXT, at TEXT);
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

    def _run_openai(self, lines):
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        r = subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"),
                            logpath, "--provider", "openai"],
                           input="".join(json.dumps(x) + "\n" for x in lines),
                           capture_output=True, text=True)
        return r.returncode, open(logpath).read()

    def test_fmt_stream_openai_clean_stream_exits_zero(self):
        rc, _log = self._run_openai([
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {}}])
        self.assertEqual(rc, 0)

    def test_fmt_stream_openai_top_level_error_fails_the_run(self):
        # codex exits 0 even when the turn dies on an API error (e.g. a model the ChatGPT
        # account can't use) — without this, run-agent scored such a run 'succeeded' with no
        # task changes and the no-op throttle parked the role. The formatter is the seam
        # that sees the event: log it loud and exit nonzero so pipefail marks the run failed.
        rc, log = self._run_openai([
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {"type": "error", "message": "The 'nope' model is not supported with a ChatGPT account."}])
        self.assertNotEqual(rc, 0)
        self.assertIn("✗", log)
        self.assertIn("not supported", log)

    def test_fmt_stream_openai_item_error_is_a_warning_not_a_failure(self):
        # an item-level error (codex's 'model metadata not found, using fallback') is
        # advisory — the turn continues — so it surfaces but must not fail the run
        rc, log = self._run_openai([
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"id": "i0", "type": "error",
                                                 "message": "Model metadata for `x` not found."}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {}}])
        self.assertEqual(rc, 0)
        self.assertIn("⚠", log)
        self.assertIn("metadata", log)

    # --- usage sidecar: the ledger's source. Each provider reports usage in its own event and
    # shape; the formatter normalizes both into <log>.usage.json so run-agent can store it. ---
    def _sidecar(self, logpath):
        p = logpath + ".usage.json"
        self.addCleanup(lambda: os.path.exists(p) and os.unlink(p))
        return json.load(open(p)) if os.path.exists(p) else None

    def test_openai_usage_lands_in_the_sidecar(self):
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        lines = [{"type": "turn.started"},
                 {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "ok"}},
                 {"type": "turn.completed", "usage": {"input_tokens": 16276, "cached_input_tokens": 11008,
                                                      "cache_write_input_tokens": 0, "output_tokens": 5,
                                                      "reasoning_output_tokens": 0}}]
        subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"), logpath, "--provider", "openai"],
                       input="".join(json.dumps(x) + "\n" for x in lines), capture_output=True, text=True)
        u = self._sidecar(logpath)
        self.assertIsNotNone(u)
        # input_tokens = the whole prompt (codex's figure already includes the cached part)
        self.assertEqual((u["input_tokens"], u["cache_read_tokens"], u["cache_write_tokens"], u["output_tokens"]),
                         (16276, 11008, 0, 5))
        self.assertIsNone(u["cost_usd"])          # codex reports no dollar figure
        self.assertEqual(u["turns"], 1)

    def test_anthropic_result_usage_lands_in_the_sidecar(self):
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        result = {"type": "result", "subtype": "success", "duration_ms": 91000, "num_turns": 12,
                  "session_id": "82de652a-0867-47ed-b0f8-67957a6faf80", "total_cost_usd": 0.0462425,
                  "usage": {"input_tokens": 10, "cache_creation_input_tokens": 22343,
                            "cache_read_input_tokens": 13615, "output_tokens": 37}}
        subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"), logpath],
                       input=json.dumps(result) + "\n", capture_output=True, text=True)
        u = self._sidecar(logpath)
        self.assertIsNotNone(u)
        # input_tokens = the whole prompt: claude's input_tokens EXCLUDES the cache figures
        self.assertEqual(u["input_tokens"], 10 + 22343 + 13615)
        self.assertEqual((u["cache_read_tokens"], u["cache_write_tokens"], u["output_tokens"]), (13615, 22343, 37))
        self.assertAlmostEqual(u["cost_usd"], 0.0462425)
        self.assertEqual(u["turns"], 12)
        self.assertEqual(u["session_id"], "82de652a-0867-47ed-b0f8-67957a6faf80")

    def test_no_usage_event_writes_no_sidecar(self):
        # a run that died before any usage report (API error) leaves nothing to store —
        # run-agent must read "absent" as NULLs, never as zeros
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"), logpath],
                       input=json.dumps({"type": "assistant", "message": {"content": [
                           {"type": "text", "text": "API Error: Unable to connect"}]}}) + "\n",
                       capture_output=True, text=True)
        self.assertIsNone(self._sidecar(logpath))

    def test_claude_cap_stops_are_logged_and_fail_the_run(self):
        # --max-turns / --max-budget-usd end the run with an error_* result subtype; the run
        # did not finish its unit, so it must land as 'failed' (feeding the backoff gate),
        # with the cap named in the log
        for sub, word in (("error_max_turns", "max turns"), ("error_max_budget_usd", "budget")):
            with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
                logpath = lf.name
            self.addCleanup(os.unlink, logpath)
            self.addCleanup(lambda p=logpath: os.path.exists(p + ".usage.json") and os.unlink(p + ".usage.json"))
            r = subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"), logpath],
                               input=json.dumps({"type": "result", "subtype": sub, "num_turns": 25,
                                                 "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n",
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, sub)
            log = open(logpath).read()
            self.assertIn("✗", log); self.assertIn(word, log)

    def test_skill_calls_log_the_skill_name(self):
        # 293 Skill calls in the workspace logs and not one says WHICH skill — the hint picked
        # command/file_path/… and Skill's input has neither. The lean profile's plugin
        # allowlists are built from this evidence.
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "browse", "args": "https://x"}}]}})
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"), logpath],
                       input=line + "\n", capture_output=True, text=True)
        self.assertIn("🔧 Skill browse", open(logpath).read())

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

    def test_stacked_same_role_slots_pair_oldest_run_first(self):
        # regression: role concurrency:2 produces TWO lock slots for the SAME agent
        # ('writer' + 'writer.2'). load_snapshot must pair each slot with its OWN running
        # run (oldest run -> first slot), not re-query "the newest running run" per slot
        # (which collapsed both slots onto the same run/timestamp/task).
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "voic")
            os.makedirs(pdir)
            mypid = os.getpid()   # a PID this process can assert is alive, without mocking
            open(os.path.join(pdir, ".lock-writer"), "w").write(f"{mypid}\n")
            open(os.path.join(pdir, ".lock-writer.2"), "w").write(f"{mypid}\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT INTO tasks(id,project,title,status,priority,assignee,pr_url,notes) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [("voic-6", "voic", "t1", "doing", "high", "writer", None, None),
                 ("voic-7", "voic", "t2", "doing", "high", "writer", None, None)])
            conn.executemany(
                "INSERT INTO runs(project,agent,task_id,status,summary,log_path,started_at,ended_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [("voic", "writer", "voic-6", "running", None, "/tmp/w1.log",
                  "2026-07-18 10:00:00", None),
                 ("voic", "writer", "voic-7", "running", None, "/tmp/w2.log",
                  "2026-07-18 10:05:00", None)])
            conn.commit()
            snap = d.load_snapshot(conn, root=root, now="2026-07-18 10:10:00")
            proj = {p.name: p for p in snap.projects}["voic"]
            self.assertEqual(len(proj.running), 2)
            slot0, slot1 = proj.running
            self.assertEqual(slot0[1], "2026-07-18 10:00:00")   # oldest run -> first slot
            self.assertEqual(slot1[1], "2026-07-18 10:05:00")
            self.assertIsNotNone(slot0[2])
            self.assertIsNotNone(slot1[2])
            self.assertNotEqual(slot0[2], slot1[2])              # distinct run ids

    def test_snapshot_groups_tasks_by_status(self):
        conn = _seed()
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        proj = {p.name: p for p in snap.projects}["acme"]
        self.assertEqual([t.id for t in proj.tasks_by_status["ready_to_merge"]],
                         ["cou-5a", "cou-9"])
        self.assertEqual(len(proj.tasks_by_status["done"]), 1)

    def test_snapshot_cap_state_mirrors_dispatcher_gate(self):
        conn = _seed()
        # seeded: cap at 20:10, success at 20:38. A success AFTER the last cap proves the window
        # is back (the badge mirrors dispatch.sh's gate), so NO cooling even within 90m of the cap.
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertFalse(snap.cap_state)
        # a cap NEWER than the latest success -> cooling ...
        conn.execute("INSERT INTO runs(project,agent,status,log_path,started_at) "
                     "VALUES('acme','qa','capped','/tmp/c2.log','2026-06-26 20:44:00')")
        snap2 = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertTrue(snap2.cap_state)
        self.assertEqual(snap2.cooling, ["anthropic"])   # NULL provider = the Claude era
        # ... but only for 90 minutes
        snap3 = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 23:59:00")
        self.assertFalse(snap3.cap_state)
        self.assertEqual(snap3.cooling, [])

    # --- spend limits (plan 1.6) on the board ---------------------------------------------
    def _budget_root(self, project_yaml="", dais_yaml=""):
        root = tempfile.mkdtemp(prefix="dais-bud-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "projects", "acme"))
        with open(os.path.join(root, "projects", "acme", "project.yaml"), "w") as f:
            f.write("project: acme\nrepo: x\nstage_goal: g\n" + project_yaml)
        if dais_yaml:
            with open(os.path.join(root, "dais.yaml"), "w") as f:
                f.write(dais_yaml)
        return root

    def test_snapshot_flags_a_task_over_its_spend_ceiling(self):
        root = self._budget_root("task_max_runs: 1\n")
        conn = _seed()
        conn.execute("UPDATE runs SET input_tokens=120000 WHERE id=1")
        conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(1,'cou-7','claim')")
        snap = d.load_snapshot(conn, root=root, now="2026-06-26 20:45:00")
        t = {x.id: x for x in snap.projects[0].tasks_by_status["ready"]}["cou-7"]
        self.assertEqual(t.over_budget, {"runs": 1, "tokens": 120000})
        out = d.render_plain(snap, color=False)
        self.assertIn("over budget", out)
        self.assertIn("cou-7", out)
        self.assertIn("--budget-lift", out)

    def test_snapshot_daily_budget_state(self):
        root = self._budget_root(dais_yaml="workspace: w\ndaily_budget: 100k\n")
        conn = _seed()
        conn.execute("UPDATE runs SET input_tokens=150000 WHERE id=1")     # started 2026-06-26
        snap = d.load_snapshot(conn, root=root, now="2026-06-26 20:45:00")
        self.assertEqual((snap.budget["limit"], snap.budget["unit"], snap.budget["spent"], snap.budget["over"]),
                         (100000, "tokens", 150000, True))
        snap2 = d.load_snapshot(conn, root=root, now="2026-06-27 10:00:00")   # a new day
        self.assertFalse(snap2.budget["over"])
        self.assertIsNone(d.load_snapshot(_seed(), root="/nonexistent", now="2026-06-26 20:45:00").budget)

    def test_parse_dry_run_finds_would_run_lines(self):
        # plan 4.3: the vitals "next:" preview parses a real dry-run tick
        text = ("tick: pool width 2\ntick[acme]: WOULD run engineer  (prio 1, last_run never)\n"
                "tick[wb]: cooling — anthropic hit its usage cap within 90m — skipping lead\n"
                "tick[wb]: WOULD run qa  (prio 100, last_run 2026-09-09 10:00:00)\n")
        self.assertEqual(d.parse_dry_run(text), [("acme", "engineer"), ("wb", "qa")])
        self.assertEqual(d.parse_dry_run("tick: nothing eligible to run\n"), [])

    def test_gate_age_reads_state_entered_at_not_updated_at(self):
        # plan 2.3: a note on a 3-day-old gate must not make it read as fresh
        conn = _seed()
        conn.execute("INSERT INTO tasks(id,project,title,status,priority,updated_at,state_entered_at) "
                     "VALUES('cou-42','acme','old gate','proposal_review','high',"
                     "'2026-06-26 20:44:00','2026-06-23 20:00:00')")
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertEqual(d.oldest_gate_age(snap, "2026-06-26 20:45:00"), "3d")
        t = {x.id: x for x in snap.projects[0].tasks_by_status["proposal_review"]}["cou-42"]
        self.assertEqual(d.fmt_age(d.entered_at(t), "2026-06-26 20:45:00"), "3d")
        # NULL (pre-0011) falls back to updated_at
        conn.execute("UPDATE tasks SET state_entered_at=NULL WHERE id='cou-42'")
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertEqual(d.oldest_gate_age(snap, "2026-06-26 20:45:00"), "1m")  # updated_at is 1m old

    # --- "why idle" (plan 1.7): the tick journal, attributed per project ---------------------
    JOURNAL = ("[2026-06-26 20:00:00] launch acme/qa (serial)\n"
               "[2026-06-26 20:30:00] throttle acme/lead — last run was a recent no-op; cooling 45m (trying next role)\n"
               "[2026-06-26 20:31:00] idle-check: skipping wb/lead — board unchanged since its last run 5.2h ago (heartbeat 24h)\n"
               "[2026-06-26 20:40:00] cap cooldown: anthropic\n"
               "[2026-06-26 20:40:00] cooling — anthropic hit its usage cap within 90m; skipping acme/engineer\n"
               "[2026-06-26 20:41:00] daily budget spent for wb: 150k/100k tokens — skipping\n")

    def _journal_root(self, text=JOURNAL):
        root = tempfile.mkdtemp(prefix="dais-tj-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "projects"))
        with open(os.path.join(root, "projects", ".watch.log"), "w") as f:
            f.write(text)
        return root

    def test_tick_journal_attributes_the_newest_line_per_project(self):
        j = d.tick_journal(self._journal_root(), ["acme", "wb", "ghost"], now_local="2026-06-26 20:45:00")
        self.assertEqual(j["projects"]["acme"]["ts"], "2026-06-26 20:40:00")
        self.assertIn("cooling", j["projects"]["acme"]["text"])
        self.assertEqual(j["projects"]["wb"]["ts"], "2026-06-26 20:41:00")
        self.assertIn("budget", j["projects"]["wb"]["text"])
        self.assertNotIn("ghost", j["projects"])
        self.assertEqual(j["workspace"]["text"], "cap cooldown: anthropic")
        self.assertEqual(j["projects"]["acme"]["age_min"], 5)

    def test_tick_reason_line_says_age_and_retry(self):
        e = {"ts": "2026-06-26 20:30:00", "age_min": 15,
             "text": "throttle acme/lead — last run was a recent no-op; cooling 45m (trying next role)"}
        line = d.tick_reason_line(e)
        self.assertIn("throttle", line); self.assertIn("15m ago", line); self.assertIn("retry ≈30m", line)
        e = {"ts": "x", "age_min": 100, "text": "STALL acme/lead — 2 consecutive no-op runs; parked until its tasks change (t-1|ready)"}
        self.assertIn("until its tasks change", d.tick_reason_line(e))
        self.assertIn("1h40m ago", d.tick_reason_line(e))

    def test_status_shows_the_last_tick_reason_under_an_idle_project(self):
        root = self._journal_root()
        snap = d.load_snapshot(_seed(), root=root, now="2026-06-26 20:45:00", now_local="2026-06-26 20:45:00")
        out = d.render_plain(snap, color=False)
        self.assertIn("last tick", out)
        self.assertIn("cooling", out)

    def test_no_journal_means_no_reason_lines(self):
        snap = d.load_snapshot(_seed(), root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertIsNone(snap.projects[0].last_tick)
        self.assertNotIn("last tick", d.render_plain(snap, color=False))

    def test_snapshot_cooling_names_only_the_capped_provider(self):
        # a codex cap after a Claude success cools openai alone — mirrors dispatch.sh's
        # per-provider gate, so the badge can't claim Claude is cooling when it isn't
        conn = _seed()
        conn.execute("INSERT INTO runs(project,agent,status,log_path,started_at,provider) "
                     "VALUES('acme','qa','capped','/tmp/c3.log','2026-06-26 20:44:00','openai')")
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertEqual(snap.cooling, ["openai"])
        self.assertTrue(snap.cap_state)
        out = d.render_plain(snap, color=False)
        self.assertIn("cooling down", out)
        self.assertIn("openai", out)

    def test_snapshot_cooling_on_a_db_without_the_provider_column(self):
        # pre-0007 db: the gate can't tell providers apart, so it cools everything — say so
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA.replace(",\n  provider TEXT", ""))
        conn.execute("INSERT INTO runs(project,agent,status,log_path,started_at) "
                     "VALUES('acme','qa','capped','/tmp/c.log','2026-06-26 20:44:00')")
        snap = d.load_snapshot(conn, root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertTrue(snap.cap_state)
        self.assertEqual(snap.cooling, ["all"])

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


class TestArchivedProjects(unittest.TestCase):
    """`archived: true` in project.yaml removes a project from the derived board — rail, WORK,
    status — without touching the db. The names ride Snapshot.archived so renderers can say
    what's hidden and how to get it back."""

    def _root(self, **projects):
        root = tempfile.mkdtemp(prefix="dais-arch-")
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        for name, yaml in projects.items():
            pdir = os.path.join(root, "projects", name)
            os.makedirs(pdir)
            with open(os.path.join(pdir, "project.yaml"), "w") as fh:
                fh.write(yaml)
        return root

    def test_archived_project_leaves_the_snapshot(self):
        # the flag must beat the disk∪tasks union: acme HAS tasks in the seeded db, and the
        # orphan-rescue path (tasks but no dir) must not resurface an explicitly archived project
        root = self._root(acme="project: acme\narchived: true\n",
                          live="project: live\n")
        snap = d.load_snapshot(_seed(), root=root, now="2026-06-26 20:45:00")
        names = [p.name for p in snap.projects]
        self.assertNotIn("acme", names)
        self.assertIn("live", names)
        self.assertEqual(snap.archived, ["acme"])

    def test_archiving_never_touches_the_db(self):
        conn = _seed()
        root = self._root(acme="project: acme\narchived: true\n")
        d.load_snapshot(conn, root=root, now="2026-06-26 20:45:00")
        n = conn.execute("SELECT COUNT(*) FROM tasks WHERE project='acme'").fetchone()[0]
        self.assertEqual(n, 4)                       # every seeded task still there

    def test_status_footer_names_the_hidden(self):
        root = self._root(acme="project: acme\narchived: true\n")
        snap = d.load_snapshot(_seed(), root=root, now="2026-06-26 20:45:00")
        text = d.render_plain(snap, color=False)
        self.assertNotIn("▌ acme", text)             # the project block is gone
        self.assertIn("archived: acme", text)        # ...but discoverable, with the way back
        self.assertIn("dais unarchive", text)

    def test_no_archived_projects_no_footer(self):
        snap = d.load_snapshot(_seed(), root="/nonexistent", now="2026-06-26 20:45:00")
        self.assertNotIn("archived:", d.render_plain(snap, color=False))


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

    def test_unknown_task_is_blank_not_a_guess(self):
        # no claim/touch/pin yet -> show nothing, never a guessed project task (even one queued).
        # The UI renders '' as a '—' placeholder; a maybe-wrong id is worse than 'not known yet'.
        m = MC.load(MC.default_machine_path())
        snap = d.Snapshot(projects=[d.Project(name="wb", stage_goal="",
            running=[("engineer", "2026-07-01 16:24:00", 1)], machine=m,
            tasks_by_status={"ready": [d.Task("wb-9", "x", "ready", "high")]},   # queued, but NOT this run's
            recent_runs=[d.Run("2026-07-01 16:24:00", "engineer", "running",
                               log_path="/tmp/e.log", id=1)])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        self.assertEqual(d.running_threads(snap, now="2026-07-01 16:30:00")[0]["task"], "")

    def test_running_thread_uses_the_dispatch_pin(self):
        # feature-1 pins runs.task_id at dispatch; the RUNNING row shows it as a fact
        m = MC.load(MC.default_machine_path())
        snap = d.Snapshot(projects=[d.Project(name="wb", stage_goal="",
            running=[("qa", "2026-07-01 16:24:00", 1)], machine=m,
            tasks_by_status={"ready": [d.Task("wb-9", "x", "ready", "high")]},   # queued decoy, not shown
            recent_runs=[d.Run("2026-07-01 16:24:00", "qa", "running",
                               log_path="/tmp/q.log", task_id="wb-3", id=1)])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        self.assertEqual(d.running_threads(snap, now="2026-07-01 16:30:00")[0]["task"], "wb-3")

    def test_running_thread_prefers_the_claim_over_the_pin(self):
        # the agent's recorded claim (what it ACTUALLY picked up) beats the dispatch-time pin
        m = MC.load(MC.default_machine_path())
        r = d.Run("2026-07-01 16:24:00", "engineer", "running", log_path="/tmp/e.log",
                  task_id="wb-1", id=1)
        r.claim = "wb-2"
        snap = d.Snapshot(projects=[d.Project(name="wb", stage_goal="",
            running=[("engineer", "2026-07-01 16:24:00", 1)], machine=m,
            tasks_by_status={}, recent_runs=[r])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        self.assertEqual(d.running_threads(snap, now="2026-07-01 16:30:00")[0]["task"], "wb-2")

    def test_each_concurrent_run_shows_its_own_pinned_task(self):
        # issue-#2 scenario, post-pin: three runs, each pinned at dispatch to its own task ->
        # no two rows share an id, and none is a project-wide guess
        m = MC.load(MC.default_machine_path())
        t0 = "2026-07-01 16:24:00"
        snap = d.Snapshot(projects=[d.Project(name="doc", stage_goal="",
            running=[("qa", t0, 1), ("lead", t0, 2), ("engineer", t0, 3)], machine=m,
            tasks_by_status={},
            recent_runs=[d.Run(t0, "qa", "running", task_id="doc-74", id=1),
                         d.Run(t0, "lead", "running", task_id="doc-80", id=2),
                         d.Run(t0, "engineer", "running", task_id="doc-90", id=3)])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        by = {t["agent"]: t["task"] for t in d.running_threads(snap, now="2026-07-01 16:30:00")}
        self.assertEqual(by, {"qa": "doc-74", "lead": "doc-80", "engineer": "doc-90"})

    def test_stacked_same_role_runs_pair_by_run_id_not_agent_name(self):
        # regression: role concurrency:2 stacks two runs of the SAME agent ('writer'). Each
        # running-tuple slot carries its OWN run_id, and running_threads must resolve each
        # slot to ITS run — not both collapsing onto whichever run agent-name lookup finds first.
        snap = d.Snapshot(projects=[d.Project(name="voic", stage_goal="",
            running=[("writer", "2026-07-18 10:00:00", 101),
                     ("writer", "2026-07-18 10:05:00", 102)],
            machine=MC.load(MC.default_machine_path()),
            tasks_by_status={},
            recent_runs=[d.Run("2026-07-18 10:00:00", "writer", "running",
                               log_path="/tmp/w1.log", task_id="voic-6", id=101),
                         d.Run("2026-07-18 10:05:00", "writer", "running",
                               log_path="/tmp/w2.log", task_id="voic-7", id=102)])],
            recent_runs=[], cap_state=False, ts="2026-07-18 10:10:00")
        threads = d.running_threads(snap, now="2026-07-18 10:10:00")
        self.assertEqual(len(threads), 2)
        by_run = {t["run_id"]: t for t in threads}
        self.assertEqual(by_run[101]["task"], "voic-6")
        self.assertEqual(by_run[101]["log_path"], "/tmp/w1.log")
        self.assertEqual(by_run[102]["task"], "voic-7")
        self.assertEqual(by_run[102]["log_path"], "/tmp/w2.log")

    def test_running_threads_collects_all_agents(self):
        snap = d.Snapshot(
            projects=[
                d.Project(name="beacon", stage_goal="",
                          running=[("engineer", "2026-06-26 20:40:00", 1)],
                          machine=MC.load(MC.default_machine_path()),
                          tasks_by_status={"doing": [d.Task("lyr-1", "t", "doing", "high")]},
                          recent_runs=[d.Run("2026-06-26 20:40:00", "engineer", "running",
                                             log_path="/tmp/x.log", task_id="lyr-1", id=1)]),
                d.Project(name="wb", stage_goal="",
                          running=[("qa", "2026-06-26 20:44:00", None)], tasks_by_status={}),
            ],
            recent_runs=[], cap_state=False, ts="2026-06-26 20:45:00")
        threads = d.running_threads(snap, now="2026-06-26 20:45:00")
        self.assertEqual(len(threads), 2)
        eng = [t for t in threads if t["agent"] == "engineer"][0]
        self.assertEqual(eng["task"], "lyr-1")
        self.assertEqual(eng["secs"], 300)
        self.assertEqual(eng["log_path"], "/tmp/x.log")

    def test_running_thread_carries_the_actual_model(self):
        # the thread carries the model the run LAUNCHED on (runs.model) — so the header can show
        # the backup when the usage-cap fallback swapped it in, not the configured primary
        snap = d.Snapshot(
            projects=[d.Project(name="wb", stage_goal="",
                                running=[("engineer", "2026-07-01 16:24:00", 1)],
                                machine=MC.load(MC.default_machine_path()),
                                tasks_by_status={},
                                recent_runs=[d.Run("2026-07-01 16:24:00", "engineer", "running",
                                                   log_path="/tmp/e.log", model="claude-opus-4-8",
                                                   id=1)])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        threads = d.running_threads(snap, now="2026-07-01 16:30:00")
        self.assertEqual(threads[0]["model"], "claude-opus-4-8")

    def test_running_header_flags_the_backup_model(self):
        app = d.App.__new__(d.App)
        app.snap = None
        app.root = "/nonexistent"                       # agent_model → configured default
        cfg, _eff = d.agent_model(app.root, "wb", "engineer")
        row = {"project": "wb", "agent": "engineer", "task_id": None,
               "since": "2026-07-01 16:24:00", "model": "claude-opus-4-8[1m]"}
        head = "\n".join(app.running_header(row, "2026-07-01 16:30:00"))
        self.assertIn("claude-opus-4-8[1m]", head)      # the ACTUAL model
        self.assertIn("BACKUP", head)                   # flagged as the fallback
        self.assertIn(cfg, head)                         # names the configured primary too

    def test_running_header_no_flag_when_on_primary(self):
        app = d.App.__new__(d.App)
        app.snap = None
        app.root = "/nonexistent"
        cfg, _eff = d.agent_model(app.root, "wb", "engineer")
        row = {"project": "wb", "agent": "engineer", "task_id": None,
               "since": "2026-07-01 16:24:00", "model": cfg}
        head = "\n".join(app.running_header(row, "2026-07-01 16:30:00"))
        self.assertIn(f"model {cfg}", head)
        self.assertNotIn("BACKUP", head)

    def test_running_thread_log_survives_a_newer_finished_run(self):
        # engineer started first and is STILL running; qa ran after it and finished, so the
        # project's newest run isn't 'running'. The engineer thread must still tail ITS OWN log
        # (found by agent), not show '(waiting for output…)' because recent_runs[0] finished.
        snap = d.Snapshot(
            projects=[d.Project(name="wb", stage_goal="",
                                running=[("engineer", "2026-07-01 16:24:00", 2)],
                                machine=MC.load(MC.default_machine_path()),
                                tasks_by_status={},
                                recent_runs=[
                                    d.Run("2026-07-01 16:25:00", "qa", "succeeded",
                                          log_path="/tmp/qa.log", id=1),
                                    d.Run("2026-07-01 16:24:00", "engineer", "running",
                                          log_path="/tmp/eng.log", id=2),
                                ])],
            recent_runs=[], cap_state=False, ts="2026-07-01 16:30:00")
        threads = d.running_threads(snap, now="2026-07-01 16:30:00")
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["log_path"], "/tmp/eng.log")

    def test_concurrent_threads_each_get_their_own_log(self):
        snap = d.Snapshot(
            projects=[d.Project(name="wb", stage_goal="",
                                running=[("engineer", "2026-07-01 16:24:00", 1),
                                         ("qa", "2026-07-01 16:25:00", 2)],
                                machine=MC.load(MC.default_machine_path()),
                                tasks_by_status={},
                                recent_runs=[
                                    d.Run("2026-07-01 16:25:00", "qa", "running",
                                          log_path="/tmp/qa.log", id=2),
                                    d.Run("2026-07-01 16:24:00", "engineer", "running",
                                          log_path="/tmp/eng.log", id=1),
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

    def test_watch_next_tick_reads_epoch_and_sleep(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 3 1751844000 10")    # draining: 10s < 900s interval
            self.assertEqual(d.watch_next_tick(root), (1751844000, 10))
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 3 1751844000")       # 4-field: epoch, no duration
            self.assertEqual(d.watch_next_tick(root), (1751844000, None))

    def test_watch_next_tick_none_pre_countdown_or_absent(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "projects"))
            self.assertEqual(d.watch_next_tick(root), (None, None))   # no pidfile
            with open(os.path.join(root, "projects", ".watch.pid"), "w") as fh:
                fh.write(f"{os.getpid()} 900 3")                      # old 3-field shape
            self.assertEqual(d.watch_next_tick(root), (None, None))

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
            return 0, "", ""

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
                                      "attest:migration_reviewed when task:touches_migrations"]}]}
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
        self.assertIn("migration_reviewed", app.dispatched)


class TestFireErrorPrefersStderr(unittest.TestCase):
    """A failed edge-script effect (machine.py's CLI script-effect path, 81e3087) must surface
    ITS reason in the panel, not machine.py's own harmless "▶ running scripts/…" progress line —
    the exact combined-output captured from a real `dais fire` of jackwangdotcom's `merge` edge
    on a task with no pr_url (merge_pr writes to stderr; machine.py prints the progress marker to
    stdout BEFORE the script even runs)."""

    _STDOUT = "  ▶ merge: running scripts/merge_pr …\n"
    _STDERR = ("merge_pr: task demo-1 has no pr_url — set it (dais task set <id> --pr <url>) "
               "or merge by hand\n  ✗ scripts/merge_pr failed — merge NOT fired; task unchanged\n")

    def test_script_stderr_wins_over_the_progress_marker(self):
        err = d._fire_error(1, self._STDOUT, self._STDERR)
        self.assertEqual(err, "merge_pr: task demo-1 has no pr_url — set it "
                              "(dais task set <id> --pr <url>) or merge by hand")
        self.assertNotIn("running scripts", err)

    def test_plain_guard_failure_is_unaffected(self):
        # no script involved — the sole stderr line (as machine.py always prints for
        # GuardFailure/ValueError) must still come through unchanged.
        err = d._fire_error(1, "", "  ✗ actor 'engineer' may not fire this edge (owner is 'founder')\n")
        self.assertEqual(err, "✗ actor 'engineer' may not fire this edge (owner is 'founder')")

    def test_no_stderr_falls_back_to_stdout(self):
        self.assertEqual(d._fire_error(3, "some stdout line\n", ""), "some stdout line")

    def test_nothing_captured_falls_back_to_exit_code(self):
        self.assertEqual(d._fire_error(2, "", ""), "exit 2")

    def test_act_machine_flash_shows_the_real_reason(self):
        # end-to-end through _act_machine's own dispatch + flash, not just the helper.
        class _FakeApp(TestActMachineConditionalAttest._FakeApp):
            def _dispatch_out(inner_self, cmd):
                inner_self.dispatched = cmd
                return 1, TestFireErrorPrefersStderr._STDOUT, TestFireErrorPrefersStderr._STDERR

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks(id TEXT, project TEXT, status TEXT, title TEXT,"
                     " assignee TEXT, parked_from TEXT, pr_url TEXT)")
        conn.execute("INSERT INTO tasks(id,project,status,title) VALUES('jac-12','jw','review','x')")
        app = _FakeApp(conn)
        machine = {"edges": [{"from": "review", "to": "done", "by": "founder", "verb": "merge",
                              "effect": {"script": {"name": "merge_pr", "outward": True}}}]}
        app._act_machine(machine, "merge", None, {"id": "jac-12", "status": "review"})
        self.assertIn("merge_pr: task demo-1 has no pr_url", app.flash)
        self.assertNotIn("running scripts", app.flash)


class TestProjectFieldBlockScalar(unittest.TestCase):
    """board.py's project_field is the python twin of lib.sh's pcfg — same line-based reader,
    same block-scalar hardening (a `key: >-` folded scalar used to come back as the literal
    string '>-' instead of the folded paragraph)."""

    def _field(self, yaml_body, key="stage_goal"):
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "demo")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "project.yaml"), "w") as fh:
                fh.write(yaml_body)
            return d.project_field(root, "demo", key)

    def test_plain_single_line_value_unchanged(self):
        got = self._field("project: demo\nstage_goal: ship the thing\nrepo: x\n")
        self.assertEqual(got, "ship the thing")

    def test_folded_block_scalar_is_joined(self):
        got = self._field(
            "project: demo\n"
            "stage_goal: >-\n"
            "  Ship the launch-week fixes and keep the\n"
            "  release lane green.\n"
            "repo: x\n")
        self.assertEqual(got, "Ship the launch-week fixes and keep the release lane green.")
        self.assertNotEqual(got, ">-")

    def test_literal_block_scalar_is_joined_too(self):
        got = self._field(
            "project: demo\n"
            "stage_goal: |\n"
            "  first line\n"
            "  second line\n"
            "repo: x\n")
        self.assertEqual(got, "first line second line")

    def test_block_scalar_stops_at_next_top_level_key(self):
        got = self._field(
            "project: demo\n"
            "stage_goal: >-\n"
            "  only this paragraph\n"
            "repo: x\n"
            "priority: 5\n")
        self.assertEqual(got, "only this paragraph")

    def test_missing_key_is_empty(self):
        self.assertEqual(self._field("project: demo\n", key="nope"), "")


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

    def test_render_project_cast_names_each_roles_provider(self):
        # a mixed cast (a codex role beside claude roles) must be readable at a glance —
        # the model id alone is the only hint otherwise, and `dais project` is where a
        # founder checks what a role will actually run on
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "p")
            os.makedirs(os.path.join(pdir, "agents"))
            with open(os.path.join(pdir, "project.yaml"), "w") as f:
                f.write("project: p\nrepo: x\nstage_goal: g\nmodel: claude-opus-4-8\n")
            with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
                f.write("---\nprovider: openai\nmodel: gpt-5.4\n---\npersona\n")
            with open(os.path.join(pdir, "agents", "engineer.md"), "w") as f:
                f.write("persona\n")
            out = d.render_project(root, "p", color=False)
            self.assertIn("openai · gpt-5.4", out)
            self.assertIn("anthropic · claude-opus-4-8", out)

    def test_render_project_names_the_cli_default_when_a_role_sets_no_model(self):
        # openai's default model comes from ~/.codex/config.toml (plan 1.8); with none readable
        # the cell must still say what happens instead of printing `openai ·  @ high`
        home = tempfile.mkdtemp(prefix="dais-nohome-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        old = os.environ.get("HOME"); os.environ["HOME"] = home
        self.addCleanup(os.environ.__setitem__, "HOME", old)
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "p")
            os.makedirs(os.path.join(pdir, "agents"))
            with open(os.path.join(pdir, "project.yaml"), "w") as f:
                f.write("project: p\nrepo: x\nstage_goal: g\n")
            with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
                f.write("---\nprovider: openai\n---\npersona\n")
            out = d.render_project(root, "p", color=False)
            self.assertIn("openai · (codex default)", out)


if __name__ == "__main__":
    unittest.main()
