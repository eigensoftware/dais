"""Cockpit tests — the TUI action engine wired into dashboard.App.

Covers the testable LOGIC: action_bar text, do_action argv (subprocess mocked),
the Enter-menu option list, +/- priority wiring, watch_args + the watch flow, and a
fake-screen render smoke that the action bar draws within the pane. Curses key-loop
glue (the modal getch loops in _menu/_confirm/_prompt) is exercised indirectly by
patching those helpers; the raw getch handling is read-verified, not unit-tested.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import dashboard as d   # harness/dashboard.py
import actions as a     # harness/actions.py


SCHEMA = """
CREATE TABLE tasks(id TEXT, project TEXT, title TEXT, status TEXT, assignee TEXT,
  priority TEXT, pr_url TEXT, notes TEXT, updated_at TEXT, blocked_on TEXT);
CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT,
  status TEXT, summary TEXT, log_path TEXT, started_at TEXT, ended_at TEXT);
"""


class FakeScr:
    """Minimal curses screen stand-in: records addstr calls, replays queued keys."""
    def __init__(self, h=24, w=100):
        self.h, self.w = h, w
        self.calls = []                # (y, x, text, attr)
        self._keys = []

    def getmaxyx(self):
        return (self.h, self.w)

    def addstr(self, y, x, s, attr=0):
        self.calls.append((y, x, s, attr))

    def erase(self):
        pass

    def clear(self):
        pass

    def refresh(self):
        pass

    def timeout(self, _t):
        pass

    def getch(self):
        return self._keys.pop(0) if self._keys else -1


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def make_app(root=None, conn=None, h=24, w=100):
    """A wired App over an in-memory DB + FakeScr; no real DB or terminal touched."""
    root = root or tempfile.mkdtemp(prefix="dais-cock-")
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    conn = conn or _conn()
    app = d.App(FakeScr(h, w), root=root, conn=conn)
    app._dais = lambda: "dais"
    app.snap = d.load_snapshot(conn, root=root)
    return app


def task_row(tid="cou-1", project="acme", status="ready", priority="medium",
             pr_url=None):
    t = d.Task(id=tid, title="a thing", status=status, priority=priority,
               assignee=None, pr_url=pr_url, notes=None)
    return dict(id=tid, kind="task", project=project, task=t, status=status,
                running=False, sel=True, label=f"  {tid}")


def project_row(name="acme"):
    return dict(id=name, kind="project", project=name, task=None, status=None,
                running=False, sel=True, label=name)


def running_row(project="acme", task_id="cou-1"):
    return dict(id=f"run::{project}", kind="running", project=project, agent="engineer",
                since="2026-06-26 20:40:00", log_path="/tmp/x.log", task_id=task_id,
                task=None, status="doing", running=True, sel=True, label="  run")


# --------------------------------------------------------------------------- #
# A — contextual action bar
# --------------------------------------------------------------------------- #
class TestActionBar(unittest.TestCase):
    def setUp(self):
        self.app = d.App.__new__(d.App)   # bar on task/project rows needs no state
        self.app.snap = None

    def _order(self, bar, *tokens):
        idxs = [bar.find(t) for t in tokens]
        for t, i in zip(tokens, idxs):
            self.assertIn(t, bar, f"{t!r} missing from {bar!r}")
        self.assertEqual(idxs, sorted(idxs), f"out of order in {bar!r}")

    def test_proposed(self):
        bar = self.app.action_bar(task_row(status="proposed"))
        self._order(bar, "a approve", "x reject")
        self.assertIn("↵ actions", bar)
        self.assertIn("n new", bar)

    def test_ready_to_merge_with_pr(self):
        bar = self.app.action_bar(
            task_row(status="ready_to_merge", pr_url="https://x/pull/42"))
        self._order(bar, "a ship", "x request-changes", "o PR")
        self.assertIn("n new", bar)

    def test_ready_to_merge_without_pr_omits_ship_and_pr(self):
        bar = self.app.action_bar(task_row(status="ready_to_merge", pr_url=None))
        self.assertNotIn("a ship", bar)
        self.assertNotIn("o PR", bar)
        self.assertIn("x request-changes", bar)

    def test_ready(self):
        bar = self.app.action_bar(task_row(status="ready"))
        self._order(bar, "a start", "x defer")
        self.assertIn("+/- priority", bar)

    def test_project_row_shows_controls(self):
        bar = self.app.action_bar(project_row())
        self._order(bar, "R run-role", "t tick", "w watch", "p pause", "c cancel")
        self.assertIn("↵ expand", bar)

    def test_running_row_reverse_is_cancel_run(self):
        bar = self.app.action_bar(running_row())
        self.assertIn("x cancel-run", bar)
        self.assertNotIn("a ", bar)         # no advance for a running row

    def test_terminal_status_minimal_set(self):
        bar = self.app.action_bar(task_row(status="done"))
        self.assertIn("+/- priority", bar)
        self.assertIn("↵ actions", bar)
        self.assertIn("n new", bar)
        self.assertNotIn("a ", bar)


# --------------------------------------------------------------------------- #
# B — do_action dispatch (subprocess mocked) + key wiring
# --------------------------------------------------------------------------- #
class TestDoAction(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.app._confirm = lambda *a: True   # default: confirms pass
        self.p = mock.patch.object(d, "subprocess")
        self.sub = self.p.start()
        self.sub.call.return_value = 0
        self.addCleanup(self.p.stop)

    def test_advance_on_proposed_approves(self):
        self.app.do_action("approve", task_row(status="proposed"))
        self.sub.call.assert_called_once_with(["dais", "approve", "cou-1"])

    def test_advance_on_ready_to_merge_ships(self):
        row = task_row(status="ready_to_merge", pr_url="https://x/y/pull/42")
        self.app.do_action("ship", row)
        self.sub.call.assert_called_once_with(["dais", "ship", "acme", "42"])

    def test_reverse_on_ready_defers(self):
        self.app.do_action("defer", task_row(status="ready"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--status", "deferred"])

    def test_confirm_no_blocks_ship(self):
        self.app._confirm = lambda *a: False
        self.app.do_action("ship", task_row(status="ready_to_merge",
                                            pr_url="https://x/y/pull/9"))
        self.sub.call.assert_not_called()

    def test_confirm_no_blocks_reject_and_cancel(self):
        self.app._confirm = lambda *a: False
        self.app.do_action("reject", task_row(status="proposed"))
        self.app.do_action("cancel", task_row(status="ready"))
        self.sub.call.assert_not_called()

    def test_confirm_yes_runs_reject(self):
        self.app.do_action("reject", task_row(status="proposed"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--status", "cancelled"])

    def test_running_reverse_cancels_the_run(self):
        self.app.do_action("cancel_run", running_row(project="beacon"))
        self.sub.call.assert_called_once_with(["dais", "cancel", "beacon"])

    def test_new_prompts_then_adds(self):
        self.app._prompt = lambda *a: "ship the funnel"
        self.app.do_action("new", project_row("beacon"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "add", "beacon", "ship the funnel"])

    def test_new_cancelled_prompt_adds_nothing(self):
        self.app._prompt = lambda *a: ""       # esc / empty
        self.app.do_action("new", project_row())
        self.sub.call.assert_not_called()

    def test_edit_title_prompts_then_sets(self):
        self.app._prompt = lambda *a: "renamed"
        self.app.do_action("edit_title", task_row(status="ready"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--title", "renamed"])

    def test_handoff_picks_role(self):
        roles_root = self.app.root
        os.makedirs(os.path.join(roles_root, "projects", "acme"), exist_ok=True)
        with open(os.path.join(roles_root, "projects", "acme", "roles"), "w") as fh:
            fh.write("qa review reactive needs_qa 1\nengineer edit reactive ready 2\n")
        self.app._menu = lambda *a, **k: 1     # pick 'engineer'
        self.app.do_action("handoff", task_row(status="ready"))
        self.sub.call.assert_called_once_with(["dais", "handoff", "cou-1", "engineer"])

    def test_set_priority_menu(self):
        self.app._menu = lambda *a, **k: 2     # PRIORITIES[2] == "high"
        self.app.do_action("set_priority", task_row(status="ready", priority="low"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--priority", "high"])


class TestKeyWiring(unittest.TestCase):
    """The `a`/`x`/`+`/`-`/`n` keys route through do_action correctly."""
    def setUp(self):
        self.app = make_app()
        self.app._confirm = lambda *a: True
        self.p = mock.patch.object(d, "subprocess")
        self.sub = self.p.start()
        self.sub.call.return_value = 0
        self.addCleanup(self.p.stop)

    def _press(self, ch, row):
        self.app.handle(ch, [row], 0, row)

    def test_a_advances_proposed(self):
        self._press(ord("a"), task_row(status="proposed"))
        self.sub.call.assert_called_once_with(["dais", "approve", "cou-1"])

    def test_x_reverses_ready(self):
        self._press(ord("x"), task_row(status="ready"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--status", "deferred"])

    def test_plus_raises_priority(self):
        self._press(ord("+"), task_row(status="ready", priority="medium"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--priority", "high"])

    def test_minus_lowers_priority(self):
        self._press(ord("-"), task_row(status="ready", priority="high"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--priority", "medium"])

    def test_a_on_project_row_is_noop(self):
        self._press(ord("a"), project_row())
        self.sub.call.assert_not_called()

    def test_b_toggles_show_parked(self):
        self.assertFalse(self.app.show_parked)
        self._press(ord("b"), task_row())
        self.assertTrue(self.app.show_parked)


class TestEnterMenu(unittest.TestCase):
    def test_menu_options_match_engine_labels(self):
        app = d.App.__new__(d.App)
        app.snap = None
        for status in ("proposed", "ready", "ready_to_merge", "backlog"):
            row = task_row(status=status, pr_url="https://x/pull/1")
            self.assertEqual(
                app.menu_options(row),
                [act.label for act in a.task_actions(status, "task", has_pr=True)],
                status)

    def test_menu_dispatches_chosen_action(self):
        # 'start' launches a streaming agent — it must be BACKGROUNDED (Popen, detached), never run
        # inline via subprocess.call (which inherits the terminal and corrupts the curses screen).
        app = make_app()
        app._confirm = lambda *a: True
        app._menu = lambda *a, **k: 0          # first action == 'start' for ready
        with mock.patch.object(d, "subprocess") as sub:
            app._action_menu(task_row(status="ready"))
            sub.Popen.assert_called_once()
            self.assertEqual(sub.Popen.call_args[0][0], ["dais", "start", "cou-1"])
            self.assertTrue(sub.Popen.call_args.kwargs.get("start_new_session"))
            sub.call.assert_not_called()


# --------------------------------------------------------------------------- #
# C — watch interval + parallelism
# --------------------------------------------------------------------------- #
class TestWatchArgs(unittest.TestCase):
    def test_passthrough(self):
        self.assertEqual(d.watch_args(900, 3), ["900", "3"])

    def test_par_clamped_high_and_low(self):
        self.assertEqual(d.watch_args(900, 9), ["900", "5"])
        self.assertEqual(d.watch_args(900, 0), ["900", "1"])

    def test_string_inputs(self):
        self.assertEqual(d.watch_args("120", "2"), ["120", "2"])

    def test_bad_interval_defaults(self):
        self.assertEqual(d.watch_args("nope", 2), ["300", "2"])
        self.assertEqual(d.watch_args(-5, 2), ["300", "2"])

    def test_bad_par_defaults_to_one(self):
        self.assertEqual(d.watch_args(60, "x"), ["60", "1"])


class TestStartWatch(unittest.TestCase):
    def test_start_builds_watch_with_interval_and_par(self):
        app = make_app()                        # fresh root → watch stopped
        app._prompt = mock.Mock(side_effect=["900", "3"])
        with mock.patch.object(d, "subprocess") as sub:
            app.start_or_stop_watch()
            args, _kw = sub.Popen.call_args
            self.assertEqual(args[0], ["dais", "watch", "900", "3"])

    def test_start_empty_prompts_fall_back_to_defaults(self):
        app = make_app()
        app._prompt = mock.Mock(side_effect=["", ""])   # accept the shown defaults
        with mock.patch.object(d, "subprocess") as sub:
            app.start_or_stop_watch()
            args, _kw = sub.Popen.call_args
            self.assertEqual(args[0], ["dais", "watch", "300", "1"])


# --------------------------------------------------------------------------- #
# render smoke — the bar draws within the pane, no overrun
# --------------------------------------------------------------------------- #
class TestDrawSmoke(unittest.TestCase):
    def _seed(self, conn):
        conn.executemany(
            "INSERT INTO tasks(id,project,title,status,priority,pr_url) VALUES(?,?,?,?,?,?)",
            [("cou-5a", "acme", "ship offer", "ready_to_merge", "high",
              "https://x/y/pull/42"),
             ("cou-7", "acme", "build", "ready", "high", None)])
        conn.commit()

    def test_action_bar_line_present_and_within_width(self):
        conn = _conn()
        self._seed(conn)
        app = make_app(conn=conn, h=24, w=100)
        app.mode = "queue"
        app.sel_id = "cou-5a"                    # a ready_to_merge row
        app.draw()
        last = [c for c in app.scr.calls if c[0] == 23]   # h-1 footer line
        self.assertTrue(last, "no footer line drawn")
        text = last[-1][2]
        self.assertIn("ship", text)             # contextual bar reflects the selection
        for (_y, x, s, _attr) in app.scr.calls:
            self.assertLessEqual(d.disp_width(s), app.scr.w - x,
                                 f"row overruns the pane: {s!r}")


# --------------------------------------------------------------------------- #
# cockpit helpers (pure: counts, loop summary, all-clear)
# --------------------------------------------------------------------------- #
def _task(tid, status, priority="medium"):
    return d.Task(id=tid, title="t", status=status, priority=priority)


def _proj(name, **counts):
    """A Project with `counts` tasks per status, ids like 'name-ready-0'."""
    tbs = {}
    for st, n in counts.items():
        tbs[st] = [_task(f"{name}-{st}-{i}", st) for i in range(n)]
    return d.Project(name=name, stage_goal="", running=[], tasks_by_status=tbs,
                     recent_runs=[])


def _snap(*projects):
    return d.Snapshot(projects=list(projects), recent_runs=[], cap_state=False,
                      ts="2026-06-29 00:00:00")


class TestCockpitHelpers(unittest.TestCase):
    def test_gate_count_sums_only_gates(self):
        snap = _snap(_proj("a", ready_to_merge=2, proposed=1, ready=5, needs_qa=3, done=9),
                     _proj("b", blocked=1, needs_review=1, changes_requested=4))
        # gates: 2 ready_to_merge + 1 proposed + 1 blocked + 1 needs_review = 5
        self.assertEqual(d.gate_count(snap), 5)

    def test_gate_count_zero(self):
        self.assertEqual(d.gate_count(_snap(_proj("a", ready=3, needs_qa=1))), 0)

    def test_gate_count_excludes_running_task(self):
        """gate_count must not count a gate task that is currently in-flight (running_ids)."""
        snap = _snap(_proj("a", needs_review=2, ready_to_merge=1))
        # task id for first needs_review: "a-needs_review-0"
        running = frozenset({("a", "a-needs_review-0")})
        self.assertEqual(d.gate_count(snap, running), d.gate_count(snap) - 1)

    def test_loop_summary_text_omits_zero_segments(self):
        snap = _snap(_proj("a", ready=3, needs_qa=1), _proj("b", ready=0))
        # changes_requested=0 omitted; order is ready, needs_qa, changes_requested
        self.assertEqual(d.loop_summary(snap),
                         "⚙ the loop: 3 ready · 1 needs_qa — g for full board")

    def test_loop_summary_none_when_empty(self):
        self.assertIsNone(d.loop_summary(_snap(_proj("a", proposed=2, done=4))))

    def test_loop_summary_excludes_running_task(self):
        snap = _snap(_proj("a", needs_qa=2))
        running = frozenset({("a", "a-needs_qa-0")})   # one is mid-run → not counted
        self.assertEqual(d.loop_summary(snap, running),
                         "⚙ the loop: 1 needs_qa — g for full board")

    def test_allclear_line_running_vs_idle(self):
        self.assertEqual(d.allclear_line(2),
                         "✓ nothing needs you — the loop is running (2 in flight)")
        self.assertEqual(d.allclear_line(0), "✓ nothing needs you — loop idle")


class TestCockpitRows(unittest.TestCase):
    def _app(self, tasks):
        """tasks: list of (id, project, title, status, priority, pr_url)."""
        conn = _conn()
        conn.executemany(
            "INSERT INTO tasks(id,project,title,status,priority,pr_url) "
            "VALUES(?,?,?,?,?,?)", tasks)
        conn.commit()
        return make_app(conn=conn)        # mode defaults to the cockpit ("queue")

    def _labels(self, rows):
        return [r["label"] for r in rows]

    def test_default_mode_is_cockpit(self):
        self.assertEqual(make_app().mode, "queue")

    def test_needs_you_banner_and_gate_rows(self):
        app = self._app([
            ("lyr-1", "beacon", "ship it", "ready_to_merge", "high", "https://x/pull/9"),
            ("cou-1", "acme", "approve me", "proposed", "high", None),
        ])
        rows = app.left_rows()
        labels = self._labels(rows)
        self.assertTrue(any("⚡ NEEDS YOU (2)" in l for l in labels), labels)
        self.assertTrue(any("⏳ ready_to_merge (1)" in l for l in labels), labels)
        self.assertTrue(any("🧭 proposed (1)" in l for l in labels), labels)
        # the gate tasks are present as selectable task rows
        gate_task_ids = {r["id"] for r in rows
                         if r["kind"] == "task" and r["status"] in d.GATE_ORDER}
        self.assertEqual(gate_task_ids, {"lyr-1", "cou-1"})

    def test_loop_work_is_collapsed_not_listed(self):
        app = self._app([
            ("cou-1", "acme", "approve me", "proposed", "high", None),
            ("win-1", "cedar", "build a", "ready", "high", None),
            ("win-2", "cedar", "build b", "ready", "medium", None),
            ("win-3", "cedar", "verify", "needs_qa", "high", None),
        ])
        rows = app.left_rows()
        # NO individual loop-status task rows
        self.assertFalse(any(r["kind"] == "task" and r["status"] in d.LOOP_SUMMARY_ORDER
                             for r in rows))
        # instead, exactly one collapsed summary line with the counts
        summaries = [r["label"] for r in rows if "⚙ the loop:" in r["label"]]
        self.assertEqual(len(summaries), 1, rows)
        self.assertIn("2 ready", summaries[0])
        self.assertIn("1 needs_qa", summaries[0])

    def test_all_clear_when_no_gates(self):
        app = self._app([
            ("win-1", "cedar", "build", "ready", "high", None),
        ])
        rows = app.left_rows()
        labels = self._labels(rows)
        self.assertFalse(any("⚡ NEEDS YOU" in l for l in labels), labels)
        self.assertTrue(any(l.startswith("✓ nothing needs you") for l in labels), labels)

    def test_g_toggles_to_board(self):
        app = make_app()
        self.assertEqual(app.mode, "queue")
        app.handle(ord("g"), [], 0, None)
        self.assertEqual(app.mode, "project")
        app.handle(ord("g"), [], 0, None)
        self.assertEqual(app.mode, "queue")


class TestCockpitHeaderAndDraw(unittest.TestCase):
    def _app(self, tasks, sel=None, w=100, h=24):
        conn = _conn()
        conn.executemany(
            "INSERT INTO tasks(id,project,title,status,priority,pr_url) "
            "VALUES(?,?,?,?,?,?)", tasks)
        conn.commit()
        app = make_app(conn=conn, h=h, w=w)
        if sel:
            app.sel_id = sel
        return app

    def _header_text(self, app):
        app.draw()
        top = [c for c in app.scr.calls if c[0] == 0]      # y == 0 is the header
        self.assertTrue(top, "no header drawn")
        return top[-1][2]

    def test_header_shows_gate_count(self):
        app = self._app([
            ("cou-1", "acme", "approve", "proposed", "high", None),
            ("lyr-1", "beacon", "ship", "ready_to_merge", "high", "https://x/pull/3"),
        ])
        self.assertIn("⚡2", self._header_text(app))

    def test_header_omits_token_when_no_gates(self):
        app = self._app([("win-1", "cedar", "build", "ready", "high", None)])
        self.assertNotIn("⚡", self._header_text(app))

    def test_full_frame_smoke_within_width(self):
        app = self._app([
            ("lyr-1", "beacon", "ship the funnel", "ready_to_merge", "high",
             "https://x/y/pull/42"),
            ("cou-1", "acme", "approve direction", "proposed", "high", None),
            ("win-1", "cedar", "build a", "ready", "high", None),
            ("win-2", "cedar", "verify b", "needs_qa", "medium", None),
        ], sel="lyr-1")
        app.draw()
        all_text = "\n".join(c[2] for c in app.scr.calls)
        self.assertIn("⚡ NEEDS YOU", all_text)
        self.assertIn("⚙ the loop:", all_text)
        footer = [c for c in app.scr.calls if c[0] == app.scr.h - 1]
        self.assertTrue(footer and "ship" in footer[-1][2], "action bar missing/wrong")
        for (_y, x, s, _attr) in app.scr.calls:
            self.assertLessEqual(d.disp_width(s), app.scr.w - x,
                                 f"row overruns the pane: {s!r}")

    def test_a_ships_a_real_cockpit_merge_row(self):
        """End-to-end: the engine still drives a gate row built by the cockpit."""
        app = self._app([
            ("lyr-1", "beacon", "ship", "ready_to_merge", "high", "https://x/y/pull/7"),
        ])
        app._confirm = lambda *a: True
        rows = app.left_rows()
        i, row = next((i, r) for i, r in enumerate(rows)
                      if r["kind"] == "task" and r["status"] == "ready_to_merge")
        app.sel_id = row["id"]
        with mock.patch.object(d, "subprocess") as sub:
            sub.call.return_value = 0
            app.handle(ord("a"), rows, i, row)
            sub.call.assert_called_once_with(["dais", "ship", "beacon", "7"])


class TestCockpitMinorCoverage(unittest.TestCase):
    """Minor coverage gaps: row ordering, parked composition, truly empty board."""

    def test_running_band_leads_gate_band(self):
        """▶ running rows must appear before the ⚡ NEEDS YOU header in left_rows()."""
        conn = _conn()
        conn.execute(
            "INSERT INTO tasks(id,project,title,status,priority,pr_url) "
            "VALUES(?,?,?,?,?,?)",
            ("prj-1", "myproj", "approve", "proposed", "high", None))
        conn.commit()
        app = make_app(conn=conn)
        thread = {"project": "myproj", "task": None, "agent": "lead",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = app.left_rows()
        running_idx = next((i for i, r in enumerate(rows) if r["kind"] == "running"), None)
        gate_idx = next((i for i, r in enumerate(rows)
                         if "⚡ NEEDS YOU" in r.get("label", "")), None)
        self.assertIsNotNone(running_idx, "no running row found")
        self.assertIsNotNone(gate_idx, "no ⚡ NEEDS YOU header found")
        self.assertLess(running_idx, gate_idx, "running band should precede gate band")

    def test_show_parked_reveals_backlog_and_deferred_alongside_gates(self):
        """Toggling `b` reveals backlog + deferred rows alongside the gate band."""
        conn = _conn()
        conn.executemany(
            "INSERT INTO tasks(id,project,title,status,priority,pr_url) "
            "VALUES(?,?,?,?,?,?)",
            [("prj-1", "myproj", "future work", "backlog", "low", None),
             ("prj-2", "myproj", "parked item", "deferred", "low", None),
             ("prj-3", "myproj", "approve this", "proposed", "high", None)])
        conn.commit()
        app = make_app(conn=conn)
        app.show_parked = True
        rows = app.left_rows()
        labels = [r["label"] for r in rows]
        self.assertTrue(any("backlog (1)" in l for l in labels), labels)
        self.assertTrue(any("deferred (1)" in l for l in labels), labels)
        parked_ids = {r["id"] for r in rows
                      if r.get("kind") == "task"
                      and r.get("status") in ("backlog", "deferred")}
        self.assertIn("prj-1", parked_ids)
        self.assertIn("prj-2", parked_ids)

    def test_empty_board_produces_allclear_and_zero_gate_count(self):
        """No tasks at all: gate_count is 0, left_rows() shows all-clear, draw() runs cleanly."""
        app = make_app()   # in-memory DB with no tasks seeded
        self.assertEqual(d.gate_count(app.snap), 0)
        rows = app.left_rows()
        labels = [r["label"] for r in rows]
        self.assertTrue(any(l.startswith("✓ nothing needs you") for l in labels), labels)
        app.draw()          # must not raise


class TestGateBannerHeaderConsistency(unittest.TestCase):
    """Integration: when a needs_review task is in-flight the header/banner must agree."""

    def test_running_needs_review_header_and_banner_show_no_gate(self):
        """Header ⚡ token and gate band are both absent when the only gate task is
        in the running band — the header/banner contract must hold."""
        conn = _conn()
        conn.execute(
            "INSERT INTO tasks(id,project,title,status,priority,pr_url) "
            "VALUES(?,?,?,?,?,?)",
            ("prj-1", "myproj", "review doc", "needs_review", "medium", None))
        conn.commit()
        app = make_app(conn=conn)
        thread = {"project": "myproj", "task": "prj-1", "agent": "lead",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            # header line (y == 0) must have no ⚡
            app.draw()
            top = [c for c in app.scr.calls if c[0] == 0]
            self.assertTrue(top, "no header drawn")
            self.assertNotIn("⚡", top[-1][2])
            # body must show the all-clear line
            rows = app.left_rows()
            labels = [r["label"] for r in rows]
            self.assertTrue(
                any(l.startswith("✓ nothing needs you") for l in labels), labels)


if __name__ == "__main__":
    unittest.main()
