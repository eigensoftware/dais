"""Action-engine tests — the machine-derived action bar/menu/dispatch in dashboard.App.
(The cockpit RENDERER, panel.PanelApp, is covered by test_panel.py.)

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
CREATE TABLE task_links(id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id TEXT,
  child_id TEXT, rel TEXT, at TEXT);
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
    return dict(id=f"run::{project}/engineer", kind="running", project=project, agent="engineer",
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

    def test_proposal_review(self):
        bar = self.app.action_bar(task_row(status="proposal_review"))
        self._order(bar, "a approve", "x request-changes")   # founder-gate phase
        self.assertIn("↵ actions", bar)
        self.assertIn("n new", bar)

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
        self.assertIn("e edit", bar)            # only edit-title + add-note remain
        self.assertIn("n new", bar)
        self.assertNotIn("a ", bar)

    def test_note_key_shown_on_task_rows(self):
        # notes are the founder↔agent channel — the bar must advertise the key on any task
        for status in ("ready", "proposal_review", "done"):
            self.assertIn("N note", self.app.action_bar(task_row(status=status)), status)


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
        self.sub.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        self.addCleanup(self.p.stop)

    def _fired(self, argv):
        # machine fires go through the CAPTURING seam (_dispatch_out) so a failed gate can
        # say WHY — assert on subprocess.run, not .call
        self.sub.run.assert_called_once_with(argv, capture_output=True, text=True,
                                             stdin=self.sub.DEVNULL)

    def test_advance_on_proposal_review_approves(self):
        # NO auto --verify: the panel never self-asserts a verify guard (approve's gate is
        # the founder's own confirm).
        self.app.do_action("approve", task_row(status="proposal_review"))
        self._fired(
            ["dais", "fire", "cou-1", "approve", "--by", "founder", "--confirm"])

    def test_reverse_on_ready_defers(self):
        self.app.do_action("defer", task_row(status="ready"))
        self._fired(
            ["dais", "fire", "cou-1", "defer", "--by", "founder"])

    def test_strong_guards_prompt_in_panel_and_fire(self):
        # greenlight (typed_confirm + attest): the panel prompts for the SAME explicit input
        # the CLI flags require — type the task id, then the fact name — and fires with them.
        answers = iter(["cou-1", "migration_reviewed"])
        self.app._prompt = lambda *a, **k: next(answers)
        self.app.do_action("greenlight", task_row(status="release_review"))
        self._fired(
            ["dais", "fire", "cou-1", "greenlight", "--by", "founder",
             "--typed", "cou-1", "--attest", "migration_reviewed"])

    def test_typed_mismatch_cancels_without_firing(self):
        self.app._prompt = lambda *a, **k: "wrong-id"
        self.app.do_action("greenlight", task_row(status="release_review"))
        self.sub.run.assert_not_called()
        self.assertIn("DID NOT FIRE", self.app.flash)

    def test_attest_not_given_cancels_without_firing(self):
        answers = iter(["cou-1", "nope"])          # typed ok, attestation refused
        self.app._prompt = lambda *a, **k: next(answers)
        self.app.do_action("greenlight", task_row(status="release_review"))
        self.sub.run.assert_not_called()
        self.assertIn("DID NOT FIRE", self.app.flash)

    def test_unchecked_verify_surfaces_the_command(self):
        # qa `pass` carries verify:tests_pass with no declared checker — the panel must NOT
        # self-assert it; it surfaces the exact fire command instead.
        self.app.do_action("pass", task_row(status="qa_review"))
        self.sub.call.assert_not_called()
        self.assertIn("--verify tests_pass", self.app.flash)

    def test_confirm_no_blocks_reject_and_cancel(self):
        self.app._confirm = lambda *a: False
        self.app.do_action("reject", task_row(status="proposed"))
        self.app.do_action("cancel", task_row(status="ready"))
        self.sub.call.assert_not_called()

    def test_confirm_yes_runs_reject(self):
        self.app.do_action("reject", task_row(status="proposed"))
        self._fired(
            ["dais", "fire", "cou-1", "reject", "--by", "founder", "--confirm"])

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

    def test_add_note_prompts_then_appends(self):
        self.app._prompt = lambda *a: "FOUNDER CHANGE REQUEST: tighten §8"
        self.app.do_action("add_note", task_row(status="proposal_review"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--notes",
             "FOUNDER CHANGE REQUEST: tighten §8"])

    def test_add_note_cancelled_prompt_sets_nothing(self):
        self.app._prompt = lambda *a: ""       # esc / empty
        self.app.do_action("add_note", task_row(status="ready"))
        self.sub.call.assert_not_called()

    def test_set_priority_menu(self):
        self.app._menu = lambda *a, **k: 2     # PRIORITIES[2] == "high"
        self.app.do_action("set_priority", task_row(status="ready", priority="low"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--priority", "high"])

    def _note_machine(self, guards=("note",)):
        """The coding machine with request_changes guarded `note` (deep-copied: _machine() caches)."""
        import copy
        m = copy.deepcopy(_machine())
        for e in m["edges"]:
            if e["verb"] == "request_changes":
                e["guards"] = list(guards)
        return m

    def test_note_guard_prompts_in_panel_then_rides_the_fire(self):
        self.app._machine_of = lambda row: self._note_machine()
        self.app._prompt = lambda *a: "tighten §8, drop the Thunes ask"
        self.app.do_action("request_changes", task_row(status="proposal_review"))
        self._fired(["dais", "fire", "cou-1", "request_changes", "--by", "founder",
                     "--notes", "tighten §8, drop the Thunes ask"])

    def test_note_guard_empty_prompt_aborts_the_fire(self):
        self.app._machine_of = lambda row: self._note_machine()
        self.app._prompt = lambda *a: ""       # esc
        self.app.do_action("request_changes", task_row(status="proposal_review"))
        self.sub.run.assert_not_called()
        self.assertIn("DID NOT FIRE", self.app.flash)

    def test_typed_note_counts_as_the_confirmation(self):
        # guards ["note","confirm"]: typing the note IS deliberate — --confirm rides
        # along without a second ask (same rule as typed_confirm)
        self.app._machine_of = lambda row: self._note_machine(["note", "confirm"])
        self.app._confirm = lambda *a: self.fail("must not double-ask after a typed note")
        self.app._prompt = lambda *a: "redo the header"
        self.app.do_action("request_changes", task_row(status="proposal_review"))
        self._fired(["dais", "fire", "cou-1", "request_changes", "--by", "founder",
                     "--notes", "redo the header", "--confirm"])


class TestKeyWiring(unittest.TestCase):
    """The `a`/`x`/`+`/`-`/`n` keys route through do_action correctly."""
    def setUp(self):
        self.app = make_app()
        self.app._confirm = lambda *a: True
        self.p = mock.patch.object(d, "subprocess")
        self.sub = self.p.start()
        self.sub.call.return_value = 0
        self.sub.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        self.addCleanup(self.p.stop)

    def _fired(self, argv):
        # machine fires go through the CAPTURING seam (_dispatch_out) so a failed gate can
        # say WHY — assert on subprocess.run, not .call
        self.sub.run.assert_called_once_with(argv, capture_output=True, text=True,
                                             stdin=self.sub.DEVNULL)

    def _press(self, ch, row):
        self.app.handle(ch, [row], 0, row)

    def test_a_advances_proposal_review(self):
        self._press(ord("a"), task_row(status="proposal_review"))
        self._fired(
            ["dais", "fire", "cou-1", "approve", "--by", "founder", "--confirm"])

    def test_x_reverses_ready(self):
        self._press(ord("x"), task_row(status="ready"))
        self._fired(
            ["dais", "fire", "cou-1", "defer", "--by", "founder"])

    def test_plus_raises_priority(self):
        self._press(ord("+"), task_row(status="ready", priority="medium"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--priority", "high"])

    def test_minus_lowers_priority(self):
        self._press(ord("-"), task_row(status="ready", priority="high"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--priority", "medium"])

    def test_shift_n_adds_note(self):
        # 'N' routes through the generic action-key path (distinct from 'n' = new task)
        self.app._prompt = lambda *a: "drop the Thunes ask"
        self._press(ord("N"), task_row(status="ready"))
        self.sub.call.assert_called_once_with(
            ["dais", "task", "set", "cou-1", "--notes", "drop the Thunes ask"])

    def test_a_on_project_row_is_noop(self):
        self._press(ord("a"), project_row())
        self.sub.call.assert_not_called()



class TestEnterMenu(unittest.TestCase):
    def test_menu_options_match_engine_labels(self):
        app = d.App.__new__(d.App)
        app.snap = None
        m = _machine()
        for status in ("proposed", "ready", "proposal_review", "qa_review"):
            row = task_row(status=status)
            self.assertEqual(app.menu_options(row),
                             [act.label for act in d._machine_actions(m, status)], status)

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


def _task(tid, status, priority="medium"):
    return d.Task(id=tid, title="t", status=status, priority=priority)


_MACHINE = None
def _machine():
    global _MACHINE
    if _MACHINE is None:
        import machine as MC
        _MACHINE = MC.load(MC.default_machine_path())
    return _MACHINE


def _proj(name, **counts):
    """A Project (coding machine) with `counts` tasks per status; ids 'name-status-i'."""
    tbs = {st: [_task(f"{name}-{st}-{i}", st) for i in range(n)] for st, n in counts.items()}
    return d.Project(name=name, stage_goal="", running=[], tasks_by_status=tbs,
                     recent_runs=[], machine=_machine())


def _snap(*projects):
    return d.Snapshot(projects=list(projects), recent_runs=[], cap_state=False,
                      ts="2026-06-29 00:00:00")


class TestCockpitHelpers(unittest.TestCase):
    def test_gate_count_sums_only_gates(self):
        snap = _snap(_proj("a", proposal_review=2, ready=5, qa_review=3, done=9),
                     _proj("b", release_review=1, release_failed=1, ready=4))
        # NEEDS YOU phases: 2 proposal_review + 1 release_review + 1 release_failed = 4
        self.assertEqual(d.gate_count(snap), 4)

    def test_gate_count_zero(self):
        self.assertEqual(d.gate_count(_snap(_proj("a", ready=3, qa_review=1))), 0)

    def test_gate_count_excludes_running_task(self):
        """gate_count must not count a gate task that is currently in-flight (running_ids)."""
        snap = _snap(_proj("a", proposal_review=2, release_review=1))
        running = frozenset({("a", "a-proposal_review-0")})   # one gate task is mid-run
        self.assertEqual(d.gate_count(snap, running), d.gate_count(snap) - 1)


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


