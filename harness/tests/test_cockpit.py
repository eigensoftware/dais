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
        self.assertNotIn("+/- priority", bar)   # priority is a no-op on a terminal task
        self.assertIn("e edit", bar)            # only edit-title remains
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


if __name__ == "__main__":
    unittest.main()


class TestPromptWrapping(unittest.TestCase):
    """The footer prompt wraps a long title onto multiple rows (grows upward) so you can see the
    whole thing while typing, instead of it scrolling off the right edge."""

    def test_long_title_is_fully_captured(self):
        app = make_app(h=24, w=40)
        title = "buy milk and " * 8 + "done"          # ~108 cols, far wider than 40
        app.scr._keys = [ord(c) for c in title] + [10]  # type it, then enter
        got = app._prompt("new task title")
        self.assertEqual(got, title)                    # full title, not clipped to one row

    def test_long_title_wraps_to_multiple_rows(self):
        app = make_app(h=24, w=40)
        app.scr._keys = [ord("x")] * 90 + [10]
        app._prompt("new task title")
        rows_drawn = {c[0] for c in app.scr.calls}      # distinct y coordinates written
        self.assertGreater(len(rows_drawn), 1)          # it used more than the single footer row

    def test_short_title_stays_one_row(self):
        app = make_app(h=24, w=80)
        app.scr._keys = [ord(c) for c in "quick"] + [10]
        got = app._prompt("title")
        self.assertEqual(got, "quick")


class TestConfirmPadding(unittest.TestCase):
    """The quit confirm gets the same padding as the menus: blank top/bottom + a 2-space left margin."""

    def test_confirm_is_padded(self):
        app = make_app()
        app.scr._keys = [ord("n")]                           # decline
        self.assertFalse(app._confirm("quit dais top?"))
        texts = [c[2] for c in app.scr.calls]
        msg = next(t for t in texts if "quit dais top?" in t)
        self.assertIn("[y/N]", msg)
        self.assertTrue(msg.startswith("  "))                # 2-space left margin
        self.assertTrue(any(t.strip() == "" for t in texts))  # has blank padding row(s)


