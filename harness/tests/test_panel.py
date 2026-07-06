import curses
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import panel as pn


def _covers_no_overlap(rects, w, h):
    """Every cell claimed by at most one rect; all rects inside the screen."""
    seen = set()
    for r in rects.values():
        assert r.y >= 0 and r.x >= 0 and r.y + r.h <= h and r.x + r.w <= w, (r, w, h)
        for yy in range(r.y, r.y + r.h):
            for xx in range(r.x, r.x + r.w):
                assert (yy, xx) not in seen, f"overlap at {(yy,xx)}"
                seen.add((yy, xx))
    return seen


class TestLayout(unittest.TestCase):
    def test_two_columns_when_wide(self):
        L = pn.layout(200, 50, n_rail_items=6)
        self.assertEqual(set(L), {"vitals", "rail", "work", "inspector", "feed", "bar"})
        _covers_no_overlap(L, 200, 50)
        self.assertEqual(L["vitals"].y, 0)
        self.assertEqual(L["bar"].y, 49)
        # left column stacks PROJECTS over WORK (same x); INSPECTOR is the right column
        self.assertEqual(L["rail"].x, 0)
        self.assertEqual(L["work"].x, 0)
        self.assertLess(L["rail"].y, L["work"].y)
        self.assertEqual(L["work"].y, L["rail"].y + L["rail"].h + pn._LEFT_GAP)  # blank row above WORK
        self.assertGreater(L["inspector"].x, 0)
        self.assertEqual(L["inspector"].x, L["rail"].w)     # inspector starts where the left col ends

    def test_medium_keeps_rail_and_inspector(self):
        # the whole point: at 105 cols the rail no longer vanishes
        L = pn.layout(105, 40, n_rail_items=6)
        self.assertEqual(set(L), {"vitals", "rail", "work", "inspector", "feed", "bar"})
        _covers_no_overlap(L, 105, 40)
        self.assertEqual(L["rail"].x, 0)
        self.assertEqual(L["work"].x, 0)
        self.assertGreater(L["inspector"].x, 0)

    def test_narrow_stacks_all_three_no_drop(self):
        L = pn.layout(70, 30, n_rail_items=4)
        self.assertEqual(set(L), {"vitals", "rail", "work", "inspector", "feed", "bar"})
        _covers_no_overlap(L, 70, 30)
        # single column: all at x=0, stacked PROJECTS -> WORK -> INSPECTOR
        self.assertEqual(L["rail"].x, 0)
        self.assertEqual(L["work"].x, 0)
        self.assertEqual(L["inspector"].x, 0)
        self.assertLess(L["rail"].y, L["work"].y)
        self.assertLess(L["work"].y, L["inspector"].y)

    def test_short_height_drops_feed(self):
        L = pn.layout(120, 12, n_rail_items=4)
        self.assertNotIn("feed", L)
        _covers_no_overlap(L, 120, 12)

    def test_show_feed_false_drops_feed(self):
        L = pn.layout(200, 50, show_feed=False)
        self.assertNotIn("feed", L)
        _covers_no_overlap(L, 200, 50)

    def test_projects_block_sized_to_items_work_gets_rest(self):
        few = pn.layout(200, 50, n_rail_items=3)
        many = pn.layout(200, 50, n_rail_items=10)
        self.assertLess(few["rail"].h, many["rail"].h)       # more projects -> taller PROJECTS block
        self.assertGreater(few["work"].h, many["work"].h)    # ... and shorter WORK
        self.assertGreater(many["work"].h, 0)                # WORK never vanishes
        # left column fills the band: PROJECTS + blank-gap + WORK == inspector height
        self.assertEqual(few["rail"].h + pn._LEFT_GAP + few["work"].h, few["inspector"].h)

    def test_two_col_boundary_width(self):
        # at exactly _TWO_COL_MIN_W the layout is still two side-by-side columns, tiling cleanly
        L = pn.layout(pn._TWO_COL_MIN_W, 40, n_rail_items=6)
        _covers_no_overlap(L, pn._TWO_COL_MIN_W, 40)
        self.assertEqual(L["rail"].x, 0)
        self.assertEqual(L["work"].x, 0)
        self.assertGreater(L["inspector"].x, 0)              # right column, not stacked

    def test_projects_capped_work_survives_many_projects(self):
        # a huge project list never starves WORK: PROJECTS caps at _PROJ_MAX_H, WORK keeps >=1 row
        L = pn.layout(200, 50, n_rail_items=40)
        _covers_no_overlap(L, 200, 50)
        self.assertLessEqual(L["rail"].h, pn._PROJ_MAX_H)
        self.assertGreaterEqual(L["work"].h, 1)


class TestFocus(unittest.TestCase):
    def test_focus_order_two_col(self):
        L = pn.layout(200, 50)
        self.assertEqual(pn.focus_order(L), ["rail", "work", "inspector"])

    def test_focus_order_narrow_still_all_three(self):
        L = pn.layout(70, 24)
        self.assertEqual(pn.focus_order(L), ["rail", "work", "inspector"])

    def test_cycle_wraps(self):
        order = ["rail", "work", "inspector"]
        self.assertEqual(pn.cycle_focus("rail", order, +1), "work")
        self.assertEqual(pn.cycle_focus("inspector", order, +1), "rail")
        self.assertEqual(pn.cycle_focus("rail", order, -1), "inspector")

    def test_cycle_unknown_current_returns_first(self):
        self.assertEqual(pn.cycle_focus("nope", ["work", "inspector"], +1), "work")

    def test_cycle_empty_returns_none(self):
        self.assertIsNone(pn.cycle_focus("work", [], +1))


import sqlite3
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard as d
from test_action_engine import FakeScr, _conn, make_app   # reuse the wired-App helpers


def _seed(conn, rows):
    conn.executemany(
        "INSERT INTO tasks(id,project,title,status,priority,pr_url) VALUES(?,?,?,?,?,?)",
        rows)
    conn.commit()


def _seed_with_notes(conn, rows_with_notes):
    """rows_with_notes: list of (id, project, title, status, priority, pr_url, notes)"""
    conn.executemany(
        "INSERT INTO tasks(id,project,title,status,priority,pr_url,notes) "
        "VALUES(?,?,?,?,?,?,?)",
        rows_with_notes)
    conn.commit()


class TestPaneRenderers(unittest.TestCase):
    def _panelapp(self, conn):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pr-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        app = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        app.snap = d.load_snapshot(conn, root=root); app._cp = lambda n: n * 1000
        return app

    def _app(self, rows):
        conn = _conn(); _seed(conn, rows)
        return self._panelapp(conn)

    def _app_with_notes(self, rows):
        conn = _conn(); _seed_with_notes(conn, rows)
        return self._panelapp(conn)

    def test_work_renders_rows_within_rect(self):
        import tempfile
        conn = _conn()
        _seed(conn, [("lyr-1", "beacon", "review", "proposal_review", "high", None)])
        root = tempfile.mkdtemp(prefix="dais-pwr-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        app = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        app.snap = d.load_snapshot(conn, root=root)
        scr = FakeScr(40, 200)
        rect = pn.Rect(2, 5, 20, 60)
        pn.render_work(scr, rect, app, focused=True)
        # a phase band drew, and nothing escaped the rect
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("PROPOSAL REVIEW", text)
        for (y, x, s, _a) in scr.calls:
            self.assertGreaterEqual(y, rect.y)
            self.assertLess(y, rect.y + rect.h)
            self.assertGreaterEqual(x, rect.x)
            self.assertLessEqual(pn.disp_width(s), rect.x + rect.w - x)

    def test_inspector_shows_selection_detail(self):
        app = self._app([("lyr-1", "beacon", "impl", "qa_review", "high", None)])
        app.sel_id = "lyr-1"
        scr = FakeScr(40, 200)
        rect = pn.Rect(2, 70, 20, 60)
        pn.render_inspector(scr, rect, app, focused=False)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("lyr-1", text)
        self.assertIn("qa_review", text)
        for (y, x, s, _a) in scr.calls:
            self.assertGreaterEqual(y, rect.y)
            self.assertLess(y, rect.y + rect.h)
            self.assertGreaterEqual(x, rect.x)
            self.assertLessEqual(pn.disp_width(s), rect.x + rect.w - x)

    def test_inspector_gate_state_shows_waiting_on_you(self):
        """A founder-gate selection carries a persistent '◆ waiting on YOU … since' line —
        the definitive 'did my fire take?' signal (it exists only while the gate is open)."""
        app = self._app([("lyr-1", "beacon", "impl", "proposal_review", "high", None)])
        app.sel_id = "lyr-1"
        scr = FakeScr(40, 200)
        pn.render_inspector(scr, pn.Rect(2, 70, 20, 60), app, focused=False)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("waiting on YOU", text)
        self.assertIn("since", text)

    def test_inspector_rail_focus_shows_project_setup(self):
        """Rail focused on a project -> the inspector explains the PROJECT (cast + resolved
        models + dispatch map), not the work selection. ALL keeps the classic detail."""
        app = self._app([("lyr-1", "beacon", "impl", "qa_review", "high", None)])
        pdir = os.path.join(app.root, "projects", "beacon")
        os.makedirs(os.path.join(pdir, "agents"), exist_ok=True)
        with open(os.path.join(pdir, "roles"), "w") as fh:
            fh.write("engineer    edit    reactive  -  1\n")
        with open(os.path.join(pdir, "project.yaml"), "w") as fh:
            fh.write("project: beacon\nmodel: m-proj\nmodel_engineer: m-eng-override\n")
        app.pane_focus = "rail"
        app._rail_i = 0                              # projects first, ALL last
        scr = FakeScr(40, 200)
        rect = pn.Rect(2, 70, 30, 80)
        pn.render_inspector(scr, rect, app, focused=False)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("cast", text)
        self.assertIn("m-eng-override", text)        # the RESOLVED per-role model, not the default
        self.assertNotIn("qa_review", text)          # not the task detail
        # ALL (the last rail item) shows the workspace summary
        app._rail_i = len(pn._rail_items(app)) - 1
        scr2 = FakeScr(40, 200)
        pn.render_inspector(scr2, rect, app, focused=False)
        text2 = "\n".join(c[2] for c in scr2.calls)
        self.assertIn("ALL", text2)
        self.assertIn("beacon", text2)              # one line per project
        self.assertIn("cast", text2)

    def test_inspector_rail_all_with_running_selection_no_crash(self):
        """Rail on ALL + a RUNNING selection (task=None) must render the workspace summary —
        the task formatter crashed on that row once (the first rail-view regression)."""
        app = self._app([("lyr-1", "beacon", "impl", "qa_review", "high", None)])
        app.pane_focus = "rail"
        app._rail_i = len(pn._rail_items(app)) - 1   # ALL
        run_row = {"kind": "running", "id": "run::beacon/engineer", "project": "beacon",
                   "task_id": "lyr-1", "task": None, "status": "doing", "sel": True,
                   "agent": "engineer", "since": "2026-07-02 21:00:00", "log_path": None}
        app.left_rows = lambda: [run_row]
        scr = FakeScr(40, 200)
        pn.render_inspector(scr, pn.Rect(2, 70, 30, 80), app, focused=False)   # must not raise
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("watch", text)                 # the workspace header drew

    def test_inspector_scroll_offsets_visible_window(self):
        """I1: detail_scroll must advance the visible window in render_inspector."""
        long_notes = "  ".join(f"note-line-{i}" for i in range(30))
        app = self._app_with_notes([
            ("lyr-1", "beacon", "impl", "qa_review", "high", None, long_notes),
        ])
        # Use a small rect so there are more wrapped lines than inner height
        rect = pn.Rect(0, 0, 6, 40)   # inner.h = 5 after title row

        scr0 = FakeScr(40, 200)
        app.detail_scroll = 0
        pn.render_inspector(scr0, rect, app, focused=False)
        lines0 = [c[2] for c in scr0.calls if c[0] > rect.y]  # skip title row

        scr3 = FakeScr(40, 200)
        app.detail_scroll = 3
        pn.render_inspector(scr3, rect, app, focused=False)
        lines3 = [c[2] for c in scr3.calls if c[0] > rect.y]  # skip title row

        # The two renders must produce different first content lines
        self.assertTrue(lines0 and lines3, "inspector rendered nothing")
        self.assertNotEqual(lines0[0], lines3[0],
                            "detail_scroll had no effect on inspector render")


class TestAggregateMap(unittest.TestCase):
    """The derived swept-state → aggregator-state map: before assemble fires there is no
    task_links row tying approved work to its release, so this map is how the panel names
    the vehicle ('⇢ win-204') and flags the no-release-filed stranded case."""
    def test_derives_from_aggregate_edges(self):
        m = {"edges": [
            {"from": "release_open", "to": "release_review", "by": "engineer",
             "verb": "assemble", "effect": {"aggregate": {"select": "state=approved"}}},
            {"from": "ready", "to": "doing", "by": "engineer", "verb": "claim"},
        ]}
        self.assertEqual(pn._aggregate_map(m), {"approved": "release_open"})

    def test_empty_for_no_machine_or_no_aggregates(self):
        self.assertEqual(pn._aggregate_map(None), {})
        self.assertEqual(pn._aggregate_map({"edges": [
            {"from": "ready", "to": "doing", "by": "engineer", "verb": "claim"}]}), {})


class TestChromePanes(unittest.TestCase):
    def _app(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-chrome-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        app = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        app.snap = d.load_snapshot(conn, root=root)
        app._cp = lambda n: n * 1000
        return app

    def test_vitals_has_workspace_and_gate_count_honestly(self):
        app = self._app([("cou-1", "acme", "approve", "proposal_review", "high", None)])
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), app)
        text = scr.calls[0][2]               # full header is first call; ng>0 adds a yellow overlay
        self.assertIn("DAIS", text)
        self.assertIn("1 NEED YOU", text)     # the gate count — the hero, uppercased when >0
        self.assertNotIn(">", text)           # no leftover ">0 running" quirk
        self.assertNotIn("LIVE", text)        # no hardcoded LIVE contradicting the watch badge
        self.assertNotIn("5h", text)          # NO fake budget bar

    def test_rail_lists_projects(self):
        app = self._app([("cou-1", "acme", "x", "proposed", "high", None),
                         ("lyr-1", "beacon", "y", "ready", "high", None)])
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 30, 22), app, focused=False)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("acme", text)
        self.assertIn("beacon", text)

    def test_bar_shows_contextual_actions_for_selection(self):
        app = self._app([("lyr-1", "beacon", "review", "proposal_review", "high", None)])
        app.sel_id = "lyr-1"
        scr = FakeScr(40, 200)
        pn.render_bar(scr, pn.Rect(39, 0, 1, 200), app, focus="work")
        text = scr.calls[-1][2]
        self.assertIn("approve", text)        # contextual (machine-edge) action bar reused
        self.assertIn("q quit", text)


import tempfile

class TestPanelApp(unittest.TestCase):
    def _app(self, rows, h=40, w=200):
        conn = _conn(); _seed(conn, rows)
        root = tempfile.mkdtemp(prefix="dais-panel-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        app = pn.PanelApp(FakeScr(h, w), root=root, conn=conn)
        app._dais = lambda: "dais"
        app.snap = d.load_snapshot(conn, root=root)
        return app

    def test_full_frame_draws_within_bounds(self):
        app = self._app([("lyr-1", "beacon", "review", "proposal_review", "high", None),
                         ("win-1", "cedar", "build", "ready", "high", None)])
        app.draw()
        text = "\n".join(c[2] for c in app.scr.calls)
        self.assertIn("DAIS", text)              # vitals
        self.assertIn("PROPOSAL REVIEW", text)   # work (a founder-gate phase)
        self.assertIn("q quit", text)          # bar
        for (y, x, s, _a) in app.scr.calls:
            self.assertLess(y, app.scr.h)
            self.assertLessEqual(pn.disp_width(s), app.scr.w - x)

    def test_tab_cycles_pane_focus(self):
        app = self._app([("lyr-1", "beacon", "x", "ready_to_merge", "high",
                          "https://x/pull/9")])
        app.draw()                             # establishes layout/focus
        start = app.pane_focus
        app.handle(ord("\t"), app.left_rows(), 0, None)
        self.assertNotEqual(app.pane_focus, start)

    def test_q_quits_after_confirm(self):
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        app._confirm = lambda *a: True            # confirm the quit
        self.assertFalse(app.handle(ord("q"), app.left_rows(), 0, None))

    def test_q_cancelled_stays_alive(self):
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        app._confirm = lambda *a: False           # decline the quit
        self.assertTrue(app.handle(ord("q"), app.left_rows(), 0, None))

    def test_b_is_a_no_op_here(self):
        # 'b' was the classic show/hide-parked toggle; the panel's bands are first-class,
        # so it's swallowed — the app stays alive and nothing changes.
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        self.assertTrue(app.handle(ord("b"), app.left_rows(), 0, None))   # alive

    def test_dispatch_captures_never_inherits_terminal(self):
        # a quick action's output (e.g. "updated win-105") must be CAPTURED, not printed over curses
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        with mock.patch.object(d.subprocess, "run") as run, \
             mock.patch.object(d.subprocess, "call") as call:
            run.return_value = mock.Mock(returncode=0, stdout="updated lyr-1", stderr="")
            rc = app._dispatch(["task", "set", "lyr-1", "--priority", "high"])
        self.assertEqual(rc, 0)
        run.assert_called_once()
        self.assertTrue(run.call_args.kwargs.get("capture_output"))   # captured
        call.assert_not_called()                                      # never the terminal-inheriting call

    def test_start_action_backgrounds_the_agent(self):
        # pressing `a` (start) on a ready task spawns the agent DETACHED (Popen), not inline
        app = self._app([("lyr-1", "beacon", "build", "ready", "high", None)])
        app.pane_focus = "work"; app._confirm = lambda *a: True
        app.sel_id = "lyr-1"
        rows = app.left_rows()
        i, row = next((i, r) for i, r in enumerate(rows)
                      if r["kind"] == "task" and r["status"] == "ready")
        with mock.patch.object(d.subprocess, "Popen") as popen, \
             mock.patch.object(d.subprocess, "call") as call, \
             mock.patch.object(d.subprocess, "run") as run:
            app.handle(ord("a"), rows, i, row)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["dais", "start", "lyr-1"])
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))  # detached
        call.assert_not_called(); run.assert_not_called()                 # never inline over curses



    def test_q_does_not_quit_while_filtering(self):
        """I2: pressing q while filtering must feed q into the filter, not quit."""
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        app.filtering = True
        app.filter = ""
        rows = app.left_rows()
        result = app.handle(ord("q"), rows, 0, None)
        self.assertTrue(result, "handle returned False (quit) while filtering — should stay alive")
        self.assertIn("q", app.filter, "q was not appended to filter while filtering")

    def test_render_bar_shows_filter_prompt_while_filtering(self):
        """I2: render_bar must show /<filter> when app.filtering is True."""
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        app.filtering = True
        app.filter = "win"
        scr = FakeScr(40, 200)
        pn.render_bar(scr, pn.Rect(39, 0, 1, 200), app, focus="work")
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("/win", text, "filter prompt /win not shown in bar while filtering")


class TestRenderWorkNative(unittest.TestCase):
    def _app(self, rows):
        conn = _conn(); _seed(conn, rows)
        return make_app(conn=conn, h=40, w=200)

    def test_band_bars_and_tags_render(self):
        app = self._app([("lyr-1", "beacon", "impl", "ready", "high", None),
                         ("cou-1", "acme", "post", "proposal_review", "high", None)])
        # PanelApp model: make a PanelApp to get the override + state
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=app.conn)
        papp.snap = d.load_snapshot(app.conn, root=root)
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 90), papp, focused=True)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("PROPOSAL REVIEW", text)   # a founder-gate phase band
        self.assertIn("READY", text)             # a queued phase band
        for (y, x, s, _a) in scr.calls:                # within the rect
            self.assertGreaterEqual(y, 1); self.assertLess(y, 31)
            self.assertLessEqual(pn.disp_width(s), 90 - x)

    def test_panelapp_left_rows_is_the_panel_model(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb2-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("lyr-1","beacon","x","proposal_review","high",None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        rows = papp.left_rows()
        self.assertTrue(any(r["kind"] == "band" and "PROPOSAL REVIEW" in r["label"] for r in rows))
        self.assertTrue(any(r["kind"] == "task" and r["id"] == "lyr-1" for r in rows))


class TestRailSelection(unittest.TestCase):
    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb3-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        return papp

    def test_rail_jk_sets_project_filter(self):
        papp = self._papp([("lyr-1","beacon","x","ready_to_merge","high","https://x/pull/9"),
                           ("cou-1","acme","y","proposed","high",None)])
        papp.pane_focus = "rail"
        papp.draw()                                   # establishes rail order
        # move down from ALL to the first project, filter should be set to a real project
        papp.handle(ord("j"), papp.left_rows(), 0, None)
        self.assertIsNotNone(papp.project_filter)
        self.assertIn(papp.project_filter, {"beacon", "acme"})
        # work is now limited to that project
        projs = {r["project"] for r in papp.left_rows() if r["kind"] == "task"}
        self.assertEqual(projs, {papp.project_filter})

    def test_g_toggles_expanded(self):
        papp = self._papp([("w-1","cedar","x","ready","high",None)])
        self.assertFalse(papp._panel_expanded)
        papp.handle(ord("g"), papp.left_rows(), 0, None)
        self.assertTrue(papp._panel_expanded)

    def test_rail_renders_all_and_projects(self):
        papp = self._papp([("lyr-1","beacon","x","ready","high",None)])
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 30, 22), papp, focused=True)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("ALL", text)
        self.assertIn("beacon", text)


class TestInspectorWideColor(unittest.TestCase):
    def test_inspector_is_a_wide_right_column(self):
        L = pn.layout(200, 50)
        # the inspector is a generous right column (the founder wanted it wide)
        self.assertGreaterEqual(L["inspector"].w, 80)

    def test_two_columns_split_50_50(self):
        # founder decision 2026-07-02: the two columns split evenly — the inspector earned half
        # the screen (model line, edges, links, live log). No left-column cap.
        for w, h in ((213, 64), (141, 50), (100, 40)):
            L = pn.layout(w, h, n_rail_items=6)
            _covers_no_overlap(L, w, h)
            self.assertEqual(L["work"].w, w // 2)             # left half (floor)
            self.assertEqual(L["inspector"].w, w - w // 2)    # right half absorbs the odd column
            self.assertEqual(L["inspector"].x, L["work"].w)

    def test_inspector_colorizes_status_and_sections(self):
        # has_color path: stub _cp to tag lines so we can assert color was applied
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb4-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn()
        conn.execute("INSERT INTO tasks(id,project,title,status,priority,pr_url,notes) "
                     "VALUES(?,?,?,?,?,?,?)",
                     ("lyr-1","beacon","ship","ready_to_merge","high","https://x/pull/9",
                      "QA PASS. all good. FAIL none."))
        conn.commit()
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root); papp.sel_id = "lyr-1"
        papp._cp = lambda n: n * 1000             # make attrs identifiable by value
        scr = FakeScr(40, 200)
        pn.render_inspector(scr, pn.Rect(1, 0, 30, 60), papp, focused=False)
        # the status line carries a non-zero (colored) attr
        status_calls = [c for c in scr.calls if "ready_to_merge" in c[2]]
        self.assertTrue(status_calls and status_calls[0][3] != 0)

    def test_inspector_running_selection_does_not_crash(self):
        """A selected RUNNING row (task=None) must show the agent header, not crash
        (detail_lines would do task.id on None)."""
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb4r-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("w-1", "cedar", "build", "ready", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        thread = {"project": "cedar", "task": "w-1", "agent": "engineer",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
            run_row = next(r for r in rows if r["kind"] == "running")
            papp.sel_id = run_row["id"]
            scr = FakeScr(40, 200)
            pn.render_inspector(scr, pn.Rect(1, 0, 30, 60), papp, focused=False)  # must not raise
            text = "\n".join(c[2] for c in scr.calls)
            self.assertIn("engineer", text)


class TestBarAndHelp(unittest.TestCase):
    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb5-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        return papp

    def test_bar_only_advertises_working_keys(self):
        papp = self._papp([("lyr-1","beacon","ship","ready_to_merge","high","https://x/pull/9")])
        papp.sel_id = "lyr-1"
        scr = FakeScr(40, 200)
        pn.render_bar(scr, pn.Rect(39, 0, 1, 200), papp, focus="work")
        text = scr.calls[-1][2]
        self.assertNotIn(": command", text)        # palette not built → not advertised
        for k in ("tab", "g expand", "/ filter", "? help", "q quit",
                  "w watch", "R run", "t tick"):    # the loop/run controls are hinted now
            self.assertIn(k, text)
        self.assertNotIn("b parked", text)          # backlog/deferred are first-class now

    def test_question_mark_toggles_help_and_draw_shows_it(self):
        papp = self._papp([("lyr-1","beacon","x","ready","high",None)])
        self.assertFalse(papp.show_help)
        papp.handle(ord("?"), papp.left_rows(), 0, None)
        self.assertTrue(papp.show_help)
        papp.draw()
        text = "\n".join(c[2] for c in papp.scr.calls)
        self.assertIn("KEYS", text)                # the overlay title
        self.assertIn("tab", text)
        self.assertIn("LOOP / RUN", text)          # the loop/run controls section
        self.assertIn("start / stop the watch loop", text)
        # any key closes
        papp.handle(ord("x"), papp.left_rows(), 0, None)
        self.assertFalse(papp.show_help)

    def test_help_overlay_is_wide_enough_to_not_clip_descriptions(self):
        # the box must fit its widest help line so descriptions don't run off (e.g. on a 105-wide term)
        scr = FakeScr(64, 105)
        pn.render_help(scr, 64, 105)
        drawn = "\n".join(c[2] for c in scr.calls)
        for ln in pn._HELP_LINES:
            self.assertIn(ln.strip(), drawn, "help line clipped: %r" % ln)


class TestRolePalette(unittest.TestCase):
    def _papp(self, rows, project=None):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb6-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp.project_filter = project
        papp._cp = lambda n: n * 1000          # make color pairs identifiable by value
        return papp

    @staticmethod
    def _band_attr(scr, name):
        for c in scr.calls:
            if name in c[2] and c[2].lstrip().startswith("▌"):   # ▌ band bar
                return c[3]
        return None

    @staticmethod
    def _row_attr(scr, needle):
        for c in scr.calls:
            if needle in c[2] and not c[2].lstrip().startswith("▌"):
                return c[3]
        return None

    def _board(self):
        # acme filtered: 1 needs_review (gate) + 1 done + 1 cancelled (archive)
        return self._papp([("cou-5a", "acme", "review", "proposal_review", "high", None),
                           ("cou-1", "acme", "done1", "done", "med", None),
                           ("cou-2", "acme", "cancelled1", "cancelled", "med", None)],
                          project="acme")

    def test_active_bands_share_one_structure_color(self):
        # consistency: every NON-EMPTY band header is the SAME cyan structure bar (not a per-band
        # hue); empty bands recede to dim (the screen reads calm when nominal)
        papp = self._papp([("g-1", "p", "gate", "proposal_review", "high", None),  # NEEDS-YOU phase
                           ("l-1", "p", "loop", "ready", "med", None)])           # QUEUED phase
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=True)
        cyan = 3000 | curses.A_REVERSE | curses.A_BOLD                # _cp(3) = structure
        self.assertEqual(self._band_attr(scr, "PROPOSAL REVIEW"), cyan)  # active phases, one color
        self.assertEqual(self._band_attr(scr, "READY"), cyan)
        self.assertEqual(self._band_attr(scr, "RUNNING"), curses.A_DIM)  # empty -> recedes

    def test_archive_rows_are_dim(self):
        papp = self._board()
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 48, 100), papp, focused=False)
        self.assertEqual(self._row_attr(scr, "cou-1"), curses.A_DIM)

    def test_gate_row_is_needs_you_yellow(self):
        # a founder-gate row (a NEEDS YOU band state) carries the needs-you role, not a status hue
        papp = self._board()
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 48, 100), papp, focused=False)  # no selection highlight
        self.assertEqual(self._row_attr(scr, "cou-5a"), 4000)            # _cp(4) = needs-you yellow

    def test_selected_row_is_uniform_bright_bar(self):
        # consistency: selection is the SAME bright bar on every row, not the row's own color reversed
        papp = self._board()
        papp.sel_id = "cou-5a"
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=True)
        self.assertEqual(self._row_attr(scr, "cou-5a"), curses.A_REVERSE | curses.A_BOLD)

    def test_selected_running_row_also_uses_the_bright_bar(self):
        # the uniform selection bar applies to running rows too — green base dropped when selected
        papp = self._papp([("z-1", "zeta", "t", "ready", "med", None)])
        threads = [{"project": "zeta", "agent": "eng", "task": "z-1",
                    "since": "2026-06-29 00:00:00", "log_path": None}]
        with mock.patch.object(d, "running_threads", return_value=threads):
            papp.sel_id = "run::zeta/eng"
            scr = FakeScr(40, 200)
            pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=True)
        run_row = next(c for c in scr.calls if "RUN" in c[2] and "eng" in c[2])
        self.assertEqual(run_row[3], curses.A_REVERSE | curses.A_BOLD)

    def test_inspector_attr_rules(self):
        papp = self._papp([("z-1", "z", "t", "ready", "med", None)])
        self.assertEqual(pn._inspector_attr(papp, "12:59 gc      succeeded   2m"), 1000)  # green
        self.assertEqual(pn._inspector_attr(papp, "07:07 lead    failed      3m"), 2000)  # red
        self.assertEqual(pn._inspector_attr(papp, "notes:"), 3000 | curses.A_BOLD)        # header
        self.assertEqual(pn._inspector_attr(papp, "runs touching z-1:"), 3000 | curses.A_BOLD)
        self.assertEqual(pn._inspector_attr(papp,
                         "assignee founder · prio high · pr (none)"), 4000)         # yellow
        # known proposal sub-heads pop as structure cyan-bold; an unknown "X:" stays plain
        self.assertEqual(pn._inspector_attr(papp, "  WHAT: do the thing"), 3000 | curses.A_BOLD)
        self.assertEqual(pn._inspector_attr(papp, "  WHY NOW: it's time"), 3000 | curses.A_BOLD)
        self.assertEqual(pn._inspector_attr(papp, "  LEAD CALL: blocked"), 0)   # not in the set

    def test_inspector_note_body_and_title_stay_plain(self):
        # free-text must NOT be miscolored by substring matches (the founder's redline)
        papp = self._papp([("z-1", "z", "t", "ready", "med", None)])
        for ln in ("  email/password OPEN signup",          # 'pass' inside 'password' -> NOT green
                   "  ...then Step-8 verification. NOTE:",   # trailing ':' is NOT a section header
                   "  LEAD CALL: BLOCKED until win-103",     # 'blocked' in prose -> NOT red
                   '"fix the failing test"'):                # a title containing 'fail' -> NOT red
            self.assertEqual(pn._inspector_attr(papp, ln), 0, ln)

    def test_inspector_head_line_is_structure_cyan(self):
        # the inspector's "<id> <status>" head line is a header → cyan-bold, not a per-status hue
        papp = self._papp([("cou-5a", "acme", "review", "proposal_review", "high", None)],
                          project="acme")
        papp.sel_id = "cou-5a"
        scr = FakeScr(40, 200)
        pn.render_inspector(scr, pn.Rect(0, 0, 20, 60), papp, focused=False)
        head = next(c for c in scr.calls if c[2].strip().startswith("cou-5a"))
        self.assertEqual(head[3], 3000 | curses.A_BOLD)                 # _cp(3) = structure

    def test_vitals_need_you_is_yellow_when_positive(self):
        papp = self._papp([("g-1", "g", "t", "proposal_review", "high", None)])
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        hits = [c for c in scr.calls
                if "NEED YOU" in c[2] and c[3] == (4000 | curses.A_REVERSE | curses.A_BOLD)]
        self.assertTrue(hits)

    def test_vitals_no_yellow_overlay_when_zero(self):
        papp = self._papp([("g-1", "g", "t", "ready", "low", None)])   # 'ready' is loop, not a gate
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        self.assertFalse([c for c in scr.calls
                          if c[3] == (4000 | curses.A_REVERSE | curses.A_BOLD)])

    def test_rail_projects_are_uniform_and_running_is_green(self):
        # consistency: idle project rows all share the same plain treatment (no per-project rainbow)
        papp = self._papp([("a-1", "alpha", "x", "ready", "med", None),
                           ("b-1", "bravo", "y", "ready", "med", None)])
        # if the snapshot derives projects differently, adapt the seed so snap.projects has >=2 names
        names = [p.name for p in papp.snap.projects]
        self.assertGreaterEqual(len(names), 2)
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 20), papp, focused=False)
        # ALL and every idle project row carry the same plain attr (0) — uniform, no hue
        self.assertEqual(self._row_attr_rail(scr, "ALL"), 0)
        self.assertEqual(self._row_attr_rail(scr, names[0]), 0)
        self.assertEqual(self._row_attr_rail(scr, names[1]), 0)
        # mark the first project running -> only it turns green (cp 1) — the live role
        papp.snap.projects[0].running = [("engineer", "2026-06-29 00:00:00")]
        scr2 = FakeScr(40, 200)
        pn.render_rail(scr2, pn.Rect(1, 0, 20, 20), papp, focused=False)
        self.assertEqual(self._row_attr_rail(scr2, names[0]) & 1000, 1000)

    @staticmethod
    def _row_attr_rail(scr, needle):
        for c in scr.calls:
            if needle in c[2]:
                return c[3]
        return None


class TestInspectorModelLine(unittest.TestCase):
    """The inspector shows WHICH MODEL (and effort) a task/agent runs on, resolved exactly as
    run-agent.sh resolves it: per-role model_<role>:/effort_<role>: beats the project-wide
    model:/effort:, else the tool default. A parked state (no dispatch role) shows no line."""

    def _papp(self, status, yaml_text):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pmdl-")
        os.makedirs(os.path.join(root, "projects", "p"), exist_ok=True)
        with open(os.path.join(root, "projects", "p", "project.yaml"), "w") as fh:
            fh.write(yaml_text)
        conn = _conn()
        conn.execute("INSERT INTO tasks(id,project,title,status,priority) VALUES(?,?,?,?,?)",
                     ("t-1", "p", "title", status, "high"))
        conn.commit()
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp.sel_id = "t-1"
        return papp

    YAML = "project: p\nrepo: p\nmodel: claude-opus-4-8\neffort: high\nmodel_engineer: claude-fable-5\n"

    def test_ready_task_shows_dispatch_role_model_override(self):
        # ready dispatches the engineer (coding machine) -> the per-role override + project effort
        papp = self._papp("ready", self.YAML)
        body = "\n".join(pn._panel_detail_lines(papp, papp._selected(papp.left_rows())[1]))
        self.assertIn("runs as engineer", body)
        self.assertIn("claude-fable-5", body)
        self.assertIn("effort high", body)

    def test_qa_task_shows_project_default_model(self):
        # qa_review dispatches qa — no model_qa override -> the project-wide model
        papp = self._papp("qa_review", self.YAML)
        body = "\n".join(pn._panel_detail_lines(papp, papp._selected(papp.left_rows())[1]))
        self.assertIn("runs as qa", body)
        self.assertIn("claude-opus-4-8", body)
        self.assertNotIn("claude-fable-5", body)

    def test_parked_state_shows_no_model_line(self):
        # approved parks (no dispatch role) -> no "runs as" line
        papp = self._papp("approved", self.YAML)
        body = "\n".join(pn._panel_detail_lines(papp, papp._selected(papp.left_rows())[1]))
        self.assertNotIn("runs as", body)

    def test_running_header_shows_model_and_effort(self):
        papp = self._papp("doing", self.YAML)
        row = {"project": "p", "agent": "engineer", "task_id": "t-1",
               "since": "2026-07-01 10:00:00"}
        head = "\n".join(papp.running_header(row, "2026-07-01 10:05:00"))
        self.assertIn("claude-fable-5", head)
        self.assertIn("effort high", head)


class TestInspectorReflow(unittest.TestCase):
    def _papp(self, notes):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb7-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn()
        conn.execute("INSERT INTO tasks(id,project,title,status,priority,pr_url,notes) "
                     "VALUES(?,?,?,?,?,?,?)",
                     ("t-1", "p", "title", "ready_to_merge", "med", "https://x/pull/1", notes))
        conn.commit()
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp.sel_id = "t-1"
        return papp

    def test_notes_preserve_author_line_breaks(self):
        # the author wrote a bulleted list; the panel must keep the bullets as separate lines
        papp = self._papp("summary line\n- first bullet\n- second bullet")
        lines = pn._panel_detail_lines(papp, papp._selected(papp.left_rows())[1])
        body = "\n".join(lines)
        self.assertIn("notes:", body)
        # each bullet is its own logical line (not collapsed into one paragraph)
        self.assertTrue(any(l.strip() == "- first bullet" for l in lines))
        self.assertTrue(any(l.strip() == "- second bullet" for l in lines))

    def test_notes_break_before_known_subheads(self):
        # the proposal template (WHAT · WHY NOW · EXPECTED IMPACT …) is often one run-on line;
        # the inspector must break it into a scannable outline — each sub-head starts a line
        papp = self._papp("WHAT: do the thing. WHY NOW: it's time. EXPECTED IMPACT: big.")
        lines = pn._panel_detail_lines(papp, papp._selected(papp.left_rows())[1])
        starts = [l.strip() for l in lines if l.strip()]
        self.assertTrue(any(s.startswith("WHAT:") for s in starts))
        self.assertTrue(any(s.startswith("WHY NOW:") for s in starts))
        self.assertTrue(any(s.startswith("EXPECTED IMPACT:") for s in starts))
        # they are SEPARATE lines, not one paragraph
        self.assertFalse(any("WHAT:" in s and "WHY NOW:" in s for s in starts))

    def test_notes_are_not_prewrapped_at_56(self):
        # a single long logical note line must come back un-wrapped (one line), so render can wrap
        # it to the REAL pane width — not the classic fixed 56
        long_line = "x" * 120
        papp = self._papp(long_line)
        lines = pn._panel_detail_lines(papp, papp._selected(papp.left_rows())[1])
        self.assertTrue(any(len(l) >= 110 for l in lines))   # still ~120 wide, not chopped to 56

    def test_render_inspector_wraps_to_pane_width_not_56(self):
        long_line = "word " * 40                              # ~200 chars, one logical line
        papp = self._papp(long_line.strip())
        papp._cp = lambda n: 0
        scr = FakeScr(40, 200)
        pn.render_inspector(scr, pn.Rect(1, 0, 30, 100), papp, focused=False)  # inner.w ~98
        note_calls = [c for c in scr.calls if "word" in c[2]]
        # a 98-wide pane must produce a wrapped note line well over 56 chars
        self.assertTrue(any(len(c[2]) > 70 for c in note_calls))

    def test_render_inspector_narrow_no_midword_overflow(self):
        papp = self._papp("supercalifragilistic " * 10)
        papp._cp = lambda n: 0
        scr = FakeScr(40, 60)
        pn.render_inspector(scr, pn.Rect(1, 0, 30, 46), papp, focused=False)   # inner.w ~44
        note_calls = [c for c in scr.calls if "supercali" in c[2]]
        self.assertTrue(note_calls)
        # nothing the renderer emits exceeds the pane width (no overflow that _add must hard-clip)
        for c in note_calls:
            self.assertLessEqual(pn.disp_width(c[2]), 44)

    def test_running_selection_still_uses_header(self):
        # regression: a running row must NOT go through _panel_detail_lines (task is None)
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb7r-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("w-1", "cedar", "build", "ready", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        thread = {"project": "cedar", "task": "w-1", "agent": "engineer",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
            run_row = next(r for r in rows if r["kind"] == "running")
            papp.sel_id = run_row["id"]
            scr = FakeScr(40, 200)
            pn.render_inspector(scr, pn.Rect(1, 0, 30, 60), papp, focused=False)  # must not raise
            self.assertIn("engineer", "\n".join(c[2] for c in scr.calls))


class TestBandSpacing(unittest.TestCase):
    def _papp(self, rows, project=None):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb7b-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp.project_filter = project
        return papp

    def test_spacer_between_bands(self):
        papp = self._papp([("a-1", "p", "t", "needs_review", "high", None)])
        rows = papp.left_rows()
        kinds = [r["kind"] for r in rows]
        # the first band has no leading spacer; every later band is immediately preceded by a spacer
        band_idxs = [i for i, k in enumerate(kinds) if k == "band"]
        self.assertGreaterEqual(len(band_idxs), 2)
        self.assertNotEqual(kinds[0], "spacer")                 # no leading spacer at the very top
        for bi in band_idxs[1:]:
            self.assertEqual(kinds[bi - 1], "spacer")

    def test_spacer_rows_are_not_selectable(self):
        papp = self._papp([("a-1", "p", "t", "needs_review", "high", None)])
        rows = papp.left_rows()
        self.assertTrue(all(not r["sel"] for r in rows if r["kind"] == "spacer"))


class TestFinalReviewFixes(unittest.TestCase):
    def _papp(self, rows, project=None):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb8-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp.project_filter = project
        return papp

    def test_slash_filter_narrows_rows(self):                       # I-1
        papp = self._papp([("lyr-1", "beacon", "ship it", "ready_to_merge", "high", "https://x/pull/1"),
                           ("cou-1", "acme", "other", "ready_to_merge", "high", "https://x/pull/2")])
        papp.filter = "lyr"
        rows = papp.left_rows()
        ids = [r.get("id") for r in rows if r["kind"] == "task"]
        self.assertIn("lyr-1", ids)
        self.assertNotIn("cou-1", ids)
        self.assertTrue(all(r["kind"] in ("task", "running") for r in rows))   # filtering flattens

    def test_loop_shown_as_rows_by_default(self):
        # the loop now shows its tasks as rows in the default view — there is no collapsed summary
        papp = self._papp([("a-1", "p", "t", "ready", "med", None)])          # default (all-projects) view
        rows = papp.left_rows()
        self.assertFalse(any(r.get("id") == "__loop_sum" for r in rows))      # summary row is gone
        self.assertTrue(any(r["kind"] == "task" and r.get("id") == "a-1" for r in rows))

    def test_running_gate_task_not_double_counted(self):           # I-3
        papp = self._papp([("cou-5a", "acme", "review", "proposal_review", "high", None)])
        thread = {"project": "acme", "task": "cou-5a", "agent": "lead",
                  "since": "2026-06-29 00:00:00", "secs": 5, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
        self.assertTrue(any(r["kind"] == "running" and r.get("task_id") == "cou-5a" for r in rows))
        self.assertFalse(any(r["kind"] == "task" and r.get("id") == "cou-5a" for r in rows))

    def test_running_loop_task_not_double_counted(self):          # I-3 (loop band variant)
        papp = self._papp([("w-9", "cedar", "qa", "needs_qa", "high", None)])
        thread = {"project": "cedar", "task": "w-9", "agent": "qa",
                  "since": "2026-06-29 00:00:00", "secs": 5, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
        self.assertFalse(any(r["kind"] == "task" and r.get("id") == "w-9" for r in rows))

    def test_loop_band_count_and_rows_exclude_running_task(self):   # M-NEW-1
        # one needs_qa task running, one not: the QUEUED band header excludes the running one
        # (count = 1) AND the loop rows show only the non-running task (no double-count).
        papp = self._papp([("w-9", "cedar", "qa", "qa_review", "high", None),
                           ("w-8", "cedar", "qa2", "qa_review", "high", None)])
        thread = {"project": "cedar", "task": "w-9", "agent": "qa",
                  "since": "2026-06-29 00:00:00", "secs": 5, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
        band = next(r for r in rows if r["kind"] == "band" and "QA REVIEW" in r["label"])
        self.assertIn("QA REVIEW · 1", band["label"])  # header excludes the running task
        loop_ids = [r.get("id") for r in rows if r["kind"] == "task" and r.get("status") == "qa_review"]
        self.assertEqual(loop_ids, ["w-8"])            # only the non-running phase task is a row

    def test_inspector_running_streams_live_log_no_pager_hint(self):   # M-1 (now: live log inline)
        # a running selection streams its live log in the inspector — no "press l/L" pager hint
        papp = self._papp([("w-1", "cedar", "build", "ready", "high", None)])
        thread = {"project": "cedar", "task": "w-1", "agent": "engineer",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
            run_row = next(r for r in rows if r["kind"] == "running")
            papp.sel_id = run_row["id"]
            scr = FakeScr(40, 200)
            pn.render_inspector(scr, pn.Rect(1, 0, 30, 80), papp, focused=False)
            text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("live log", text)            # the inspector now shows the live log section
        self.assertNotIn("press l", text)
        self.assertNotIn("press L", text)

    def test_inspector_running_shows_log_tail(self):
        import tempfile
        papp = self._papp([("w-1", "cedar", "build", "ready", "high", None)])
        logf = os.path.join(tempfile.mkdtemp(prefix="dais-log-"), "agent.log")
        with open(logf, "w") as fh:
            fh.write("setting up\nrunning tests\nALL GREEN — committing\n")
        thread = {"project": "cedar", "task": "w-1", "agent": "engineer",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": logf}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
            papp.sel_id = next(r for r in rows if r["kind"] == "running")["id"]
            scr = FakeScr(40, 200)
            pn.render_inspector(scr, pn.Rect(1, 0, 30, 80), papp, focused=False)
            text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("ALL GREEN — committing", text)   # the live tail is streamed inline
        self.assertNotIn("waiting for output", text)


class TestPaneFocusIndicator(unittest.TestCase):
    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb9-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("a-1", "alpha", "t", "ready_to_merge", "high", "https://x/pull/1")])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000          # make the accent color identifiable
        return papp

    def test_focused_title_has_marker_and_bright_bar(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_pane_title(scr, pn.Rect(0, 0, 10, 40), "WORK", True)
        call = scr.calls[-1]
        self.assertIn("▶ WORK", call[2])
        self.assertEqual(call[3], curses.A_REVERSE | curses.A_BOLD)   # bright bar, no color pair

    def test_unfocused_title_is_dim_without_marker(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_pane_title(scr, pn.Rect(0, 0, 10, 40), "WORK", False)
        call = scr.calls[-1]
        self.assertNotIn("▶", call[2])
        self.assertIn("WORK", call[2])
        self.assertEqual(call[3], curses.A_DIM)

    def test_exactly_one_pane_title_marked_in_full_draw(self):
        papp = self._papp()
        papp.pane_focus = "work"
        papp.draw()
        titles = [c for c in papp.scr.calls
                  if "▶" in c[2] and any(t in c[2] for t in ("WORK", "PROJECTS", "INSPECTOR"))]
        self.assertEqual(len(titles), 1)
        self.assertIn("WORK", titles[0][2])


class TestLogWall(unittest.TestCase):
    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-lw-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("w-1", "cedar", "build", "ready", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        return papp

    def test_empty_state_when_no_agents(self):
        papp = self._papp()
        with mock.patch.object(d, "running_threads", return_value=[]):
            scr = FakeScr(40, 200)
            pn.render_logwall(scr, pn.Rect(1, 0, 30, 120), papp)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("no agents running", text)
        self.assertNotIn("press w", text)

    def test_overflow_note_reserves_last_row(self):
        papp = self._papp()
        threads = [{"project": f"p{i}", "agent": "eng", "since": "2026-06-29 00:00:00",
                    "secs": 5, "task": f"t{i}", "log_path": None} for i in range(5)]
        with mock.patch.object(d, "running_threads", return_value=threads):
            scr = FakeScr(40, 200)
            pn.render_logwall(scr, pn.Rect(1, 0, 4, 120), papp)   # title takes 1 row -> inner.h == 3
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("+3 more", text)                            # 5 agents, 2 shown, 3 hidden
        headers = [c for c in scr.calls if "running" in c[2] and "/eng" in c[2]]
        self.assertEqual(len(headers), 2)                         # exactly 2 headers (no clobber)
        note = next(c for c in scr.calls if "+3 more" in c[2])
        self.assertFalse(any(h[0] == note[0] for h in headers))   # note is on its own row

    def test_band_header_and_live_tail_with_red_errors(self):
        import tempfile
        logf = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, prefix="dais-lwlog-")
        logf.write("starting build\nrunning tests\nTraceback (most recent call last)\n")
        logf.close()
        self.addCleanup(os.unlink, logf.name)
        papp = self._papp()
        papp._cp = lambda n: n * 1000
        thread = {"project": "cedar", "agent": "engineer", "since": "2026-06-29 00:00:00",
                  "secs": 240, "task": "win-95", "log_path": logf.name}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            scr = FakeScr(40, 200)
            pn.render_logwall(scr, pn.Rect(1, 0, 30, 120), papp)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("cedar/engineer", text)
        self.assertIn("win-95", text)
        self.assertIn("running tests", text)
        err = [c for c in scr.calls if "Traceback" in c[2]]
        self.assertTrue(err and err[0][3] == 2000)            # red = _cp(2)

    def test_waiting_when_log_missing(self):
        papp = self._papp()
        thread = {"project": "cedar", "agent": "engineer", "since": "2026-06-29 00:00:00",
                  "secs": 5, "task": "win-95", "log_path": None}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            scr = FakeScr(40, 200)
            pn.render_logwall(scr, pn.Rect(1, 0, 30, 120), papp)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("waiting for output", text)


class TestLogWallMode(unittest.TestCase):
    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-lwm-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("w-1", "cedar", "build", "ready", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        return papp

    def test_L_opens_then_closes(self):
        papp = self._papp()
        self.assertFalse(papp.show_logwall)
        papp.handle(ord("L"), [], 0, None)
        self.assertTrue(papp.show_logwall)
        papp.handle(ord("L"), [], 0, None)
        self.assertFalse(papp.show_logwall)

    def test_esc_closes_wall(self):
        papp = self._papp()
        papp.show_logwall = True
        papp.handle(27, [], 0, None)
        self.assertFalse(papp.show_logwall)

    def test_q_quits_from_wall(self):
        papp = self._papp()
        papp.show_logwall = True
        papp._confirm = lambda *a: True            # confirm the quit
        self.assertFalse(papp.handle(ord("q"), [], 0, None))

    def test_draw_shows_wall_and_hides_panes(self):
        papp = self._papp()
        papp.show_logwall = True
        with mock.patch.object(d, "running_threads", return_value=[]):
            papp.draw()
        text = "\n".join(c[2] for c in papp.scr.calls)
        self.assertIn("LOG WALL", text)
        self.assertNotIn("INSPECTOR", text)
        self.assertNotIn("PROJECTS", text)


class TestLogWallBarHelp(unittest.TestCase):
    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-lwb-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("w-1", "cedar", "build", "ready", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        return papp

    def test_bar_shows_wall_keys_in_wall_mode(self):
        papp = self._papp()
        papp.show_logwall = True
        scr = FakeScr(40, 200)
        pn.render_bar(scr, pn.Rect(39, 0, 1, 200), papp, "work")
        text = scr.calls[-1][2]
        self.assertIn("L/esc back", text)
        self.assertIn("q quit", text)

    def test_normal_bar_advertises_L_logs(self):
        papp = self._papp()
        scr = FakeScr(40, 200)
        pn.render_bar(scr, pn.Rect(39, 0, 1, 200), papp, "work")
        self.assertIn("L logs", scr.calls[-1][2])

    def test_help_overlay_lists_log_wall(self):
        self.assertTrue(any("L logs" in ln for ln in pn._HELP_LINES))


class TestSplitBands(unittest.TestCase):
    def test_even_split(self):
        self.assertEqual(pn.split_bands(1, 9, 3), [(1, 3), (4, 3), (7, 3)])

    def test_remainder_frontloaded_and_contiguous(self):
        self.assertEqual(pn.split_bands(1, 20, 3), [(1, 7), (8, 7), (15, 6)])

    def test_zero_agents(self):
        self.assertEqual(pn.split_bands(1, 10, 0), [])

    def test_one_agent_full_height(self):
        self.assertEqual(pn.split_bands(2, 10, 1), [(2, 10)])

    def test_height_less_than_n_is_contiguous_and_sums(self):
        bands = pn.split_bands(0, 2, 5)
        self.assertEqual(len(bands), 5)
        self.assertEqual(sum(h for _, h in bands), 2)
        ys = [y for y, _ in bands]
        hs = [h for _, h in bands]
        for i in range(1, 5):
            self.assertEqual(ys[i], ys[i - 1] + hs[i - 1])


class TestCutRelease(unittest.TestCase):
    """C cuts a release for the selected row's project: creates the release task at the machine's
    release-open state so the engineer's next run assembles everything approved. Refuses when
    nothing is approved or when a release is already in flight."""

    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-cutrel-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def test_cut_release_dispatches_task_add_at_release_open(self):
        papp = self._papp([("a-1", "p", "shipped work", "approved", "high", None)])
        sent = []
        with mock.patch.object(papp, "_confirm", return_value=True), \
             mock.patch.object(papp, "_dispatch", side_effect=lambda c: sent.append(c) or 0):
            papp._cut_release({"project": "p"})
        self.assertEqual(len(sent), 1)
        cmd = sent[0]
        self.assertEqual(cmd[:3], ["task", "add", "p"])
        self.assertIn("--status", cmd); self.assertEqual(cmd[cmd.index("--status") + 1], "release_open")

    def test_refuses_when_nothing_approved(self):
        papp = self._papp([("a-1", "p", "queued", "ready", "high", None)])
        sent = []
        with mock.patch.object(papp, "_confirm", return_value=True), \
             mock.patch.object(papp, "_dispatch", side_effect=lambda c: sent.append(c) or 0):
            papp._cut_release({"project": "p"})
        self.assertEqual(sent, [])
        self.assertIn("nothing", papp.flash)

    def test_refuses_when_a_release_is_in_flight(self):
        papp = self._papp([("a-1", "p", "shipped work", "approved", "high", None),
                           ("rel-1", "p", "Release", "release_review", "high", None)])
        sent = []
        with mock.patch.object(papp, "_confirm", return_value=True), \
             mock.patch.object(papp, "_dispatch", side_effect=lambda c: sent.append(c) or 0):
            papp._cut_release({"project": "p"})
        self.assertEqual(sent, [])
        self.assertIn("rel-1", papp.flash)

    def test_C_key_routes_to_cut_release(self):
        papp = self._papp([("a-1", "p", "x", "approved", "high", None)])
        with mock.patch.object(papp, "_cut_release") as cr:
            papp.handle(ord("C"), [], 0, {"project": "p"})
        cr.assert_called_once()


class TestRailSelectionUniformBar(unittest.TestCase):
    """The rail CURSOR is ONE uniform bright bar (the WORK-list convention: selection is the bar,
    not the row's hue) — no yellow needs-you patch, no green live hue, no dim zero-dots inside the
    selection. All hues resume the moment the cursor moves off the row."""

    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-railsel-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("a-1", "alpha", "gate", "proposal_review", "high", None),
                                     ("a-2", "alpha", "work", "ready", "med", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp.snap.projects[0].running = [("engineer", "2026-06-29 00:00:00")]  # live -> green hue
        papp._cp = lambda n: n * 1000
        papp._rail_i = 0                                    # cursor on 'alpha' (ALL is last now)
        return papp

    def test_selected_row_is_one_uniform_bar(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 30), papp, focused=True)
        row_y = next(c[0] for c in scr.calls if "alpha" in c[2])
        bar = curses.A_REVERSE | curses.A_BOLD
        for c in scr.calls:
            if c[0] == row_y and c[2].strip():
                self.assertEqual(c[3], bar,
                                 "cell %r attr %r != the uniform selection bar" % (c[2], c[3]))

    def test_unselected_rows_keep_their_hues(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 30), papp, focused=False)   # no cursor anywhere
        cells = [c[3] for c in scr.calls if c[2].strip() == "1"]
        self.assertIn(4000 | curses.A_BOLD, cells)          # the yellow needs-you pop is back


class TestVitalsShippable(unittest.TestCase):
    """The vitals bar nudges when a project has QA-passed inventory (its machine's release pool)
    and NOTHING in its release lane — exactly when `C` would cut a release. Hidden while a release
    is anywhere in flight (the release itself carries the attention then) and when the pool is empty."""

    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-ship-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 220), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def _bar(self, papp):
        scr = FakeScr(40, 220)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 220), papp)
        return scr

    def test_token_shows_when_pool_full_and_lane_empty(self):
        scr = self._bar(self._papp([("a-1", "p", "x", "approved", "high", None),
                                    ("a-2", "p", "y", "approved", "med", None)]))
        toks = [c for c in scr.calls if "⬆ ship" in c[2]]
        self.assertTrue(toks)
        self.assertIn("p·2", toks[-1][2])
        self.assertEqual(toks[-1][3], 4000 | curses.A_BOLD)   # yellow bold, NOT the reverse alarm

    def test_hidden_while_a_release_is_in_flight(self):
        scr = self._bar(self._papp([("a-1", "p", "x", "approved", "high", None),
                                    ("r-1", "p", "Release", "release_review", "high", None)]))
        self.assertFalse([c for c in scr.calls if "⬆ ship" in c[2]])

    def test_hidden_when_pool_empty(self):
        scr = self._bar(self._papp([("a-1", "p", "x", "ready", "high", None)]))
        self.assertFalse([c for c in scr.calls if "⬆ ship" in c[2]])


class TestRailNeedsYouChip(unittest.TestCase):
    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-rail-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("a-1", "alpha", "rev", "proposal_review", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def test_needs_you_count_is_bold_yellow_and_separate(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 30), papp, focused=False)   # ≥2 cols so `you` shows
        # the table's `you` column shows the gate count (alpha has one needs_review task)
        cells = [c for c in scr.calls if c[2].strip() == "1"]
        self.assertTrue(cells)                                  # the gate count renders
        self.assertEqual(cells[0][3], 4000 | curses.A_BOLD)     # bold yellow (_cp(4)), not the name hue
        name_calls = [c for c in scr.calls if "alpha" in c[2] and "1" not in c[2]]
        self.assertTrue(name_calls)                             # count is a SEPARATE _add from the name


class TestFeedTicker(unittest.TestCase):
    def _papp(self, runs):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-feed-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=_conn())
        papp.snap = d.Snapshot(projects=[], recent_runs=runs, cap_state=False,
                               ts="2026-06-29 16:00:00", workspace="eigen")
        papp._cp = lambda n: n * 1000          # make color pairs identifiable by value
        return papp

    def _run(self, hhmm, project, agent, status):
        # load_snapshot stores agent pre-combined as 'project/agent'; mirror that here
        return d.Run(started_at=f"2026-06-29 {hhmm}:00", agent=f"{project}/{agent}",
                     status=status, project=project)

    def test_feed_shows_recent_runs_newest_first(self):
        papp = self._papp([self._run("23:00", "cedar", "qa", "succeeded"),
                           self._run("22:00", "acme", "lead", "running")])
        scr = FakeScr(40, 200)
        pn.render_feed(scr, pn.Rect(38, 0, 1, 200), papp)
        text = "".join(c[2] for c in scr.calls)
        self.assertIn("FEED", text)
        self.assertIn("cedar/qa", text)
        self.assertIn("acme/lead", text)
        self.assertLess(text.index("cedar/qa"), text.index("acme/lead"))   # newest first

    def test_feed_colors_results_by_palette(self):
        papp = self._papp([self._run("23:00", "p", "qa", "succeeded"),
                           self._run("22:00", "p", "eng", "failed")])
        scr = FakeScr(40, 200)
        pn.render_feed(scr, pn.Rect(38, 0, 1, 200), papp)
        succ = next(c for c in scr.calls if "succeeded" in c[2])
        fail = next(c for c in scr.calls if "failed" in c[2])
        self.assertEqual(succ[3], 1000)            # _cp(1) green = good/live
        self.assertEqual(fail[3], 2000)            # _cp(2) red = bad

    def test_feed_empty_state(self):
        papp = self._papp([])
        scr = FakeScr(40, 200)
        pn.render_feed(scr, pn.Rect(38, 0, 1, 200), papp)
        self.assertIn("no recent runs", "".join(c[2] for c in scr.calls))

    def test_feed_clips_within_rect(self):
        runs = [self._run("23:00", f"project-{i}", "agent", "succeeded") for i in range(20)]
        papp = self._papp(runs)
        scr = FakeScr(40, 60)
        pn.render_feed(scr, pn.Rect(38, 0, 1, 60), papp)
        for (y, x, s, _a) in scr.calls:
            self.assertEqual(y, 38)
            self.assertLessEqual(pn.disp_width(s), 60 - x)   # nothing escapes the rect


class TestRailScroll(unittest.TestCase):
    """The PROJECTS rail is capped at _PROJ_MAX_H; with more projects than fit it must scroll to
    keep the cursor reachable and badge the hidden count, instead of silently truncating the tail."""

    def _papp(self, n_projects):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-rail-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn()
        _seed(conn, [(f"p{i}-1", f"proj{i:02d}", "t", "ready", "med", None)
                     for i in range(n_projects)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        return papp

    def test_cursor_stays_visible_when_selected_past_the_window(self):
        papp = self._papp(15)
        items = pn._rail_items(papp)             # 15 projects + "ALL" (totals row, last) = 16
        papp._rail_i = len(items) - 1            # select the very last row (ALL)
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 6, 30), papp, focused=True)   # body_h = 4 (title+header)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("ALL", text)               # the tail row (ALL) is now on-screen
        self.assertNotIn(items[0], text)         # the top scrolled off to make room
        cursor = [c for c in scr.calls if (c[3] & curses.A_REVERSE) and items[-1] in c[2]]
        self.assertTrue(cursor)                  # the reverse cursor lands on the selected row

    def test_overflow_badge_counts_hidden_projects(self):
        papp = self._papp(15)                    # 16 items, body_h 4 (title+header) -> 12 hidden
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 6, 30), papp, focused=True)
        title = scr.calls[0][2]                  # the pane-title row is the first _add
        self.assertIn("PROJECTS", title)
        self.assertIn("+12", title)

    def test_no_badge_and_top_anchored_when_all_fit(self):
        papp = self._papp(4)                     # 5 items, comfortably inside body_h
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 30), papp, focused=True)
        self.assertNotIn("+", scr.calls[0][2])   # no overflow badge when nothing is hidden
        self.assertIn("ALL", "\n".join(c[2] for c in scr.calls))


class TestMissionControlDots(unittest.TestCase):
    """The status-dot vocabulary: ● running (green), ◆ needs-you (yellow), shared by vitals + rail."""

    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-mc-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def test_vitals_running_token_is_green_when_active(self):
        papp = self._papp([("z-1", "zeta", "t", "ready", "med", None)])
        threads = [{"project": "zeta", "agent": "eng", "task": "z-1",
                    "since": "2026-06-29 00:00:00", "log_path": None}]
        with mock.patch.object(d, "running_threads", return_value=threads):
            scr = FakeScr(40, 200)
            pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        hits = [c for c in scr.calls
                if "running" in c[2] and c[3] == (1000 | curses.A_REVERSE | curses.A_BOLD)]
        self.assertTrue(hits)                    # the run token is overlaid green (live) when n>0
        self.assertIn(pn._DOT_RUN, hits[0][2])   # …and carries the ● dot

    def test_vitals_calm_when_idle(self):
        papp = self._papp([("z-1", "zeta", "t", "ready", "med", None)])   # 0 running, 0 gated
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        # no green run overlay and no yellow gate overlay — the strip reads nominal
        self.assertFalse([c for c in scr.calls
                          if c[3] in (1000 | curses.A_REVERSE | curses.A_BOLD,
                                      4000 | curses.A_REVERSE | curses.A_BOLD)])

    def test_rail_running_project_shows_the_run_dot(self):
        papp = self._papp([("a-1", "alpha", "x", "ready", "med", None)])
        papp.snap.projects[0].running = [("engineer", "2026-06-29 00:00:00")]
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 30), papp, focused=False)
        row = next(c for c in scr.calls if "alpha" in c[2])
        self.assertIn(pn._DOT_RUN, row[2])       # ● marks the live project (replaces the old '>')


class TestVitalsBar(unittest.TestCase):
    """The top vitals readout has its OWN background colour (_VITALS) so it never reads as a focused
    pane title (a plain reverse bar) or a band header (cyan). The panel is one default look now —
    the mission-control skin was promoted to default and the `m` toggle removed."""

    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-vit-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def test_vitals_bar_uses_a_distinct_background(self):
        papp = self._papp([("z-1", "zeta", "t", "ready", "med", None)])
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        base = scr.calls[0][3]                                          # the full-width base bar
        self.assertEqual(base, (pn._VITALS * 1000) | curses.A_BOLD)     # its own pair (bold white on blue)
        self.assertNotEqual(base, curses.A_REVERSE | curses.A_BOLD)     # ≠ a focused pane title bar

    def test_converged_to_one_default_no_skin_flag(self):
        papp = self._papp([("z-1", "zeta", "t", "ready", "med", None)])
        self.assertFalse(hasattr(papp, "_mc"))     # one default look; the m-toggle is gone


def _seed_runs(conn, runs):
    """runs: list of (project, agent, status, summary, started_at, ended_at). Inserted in order,
    so the LAST tuple is the newest (load_runs orders by id DESC)."""
    conn.executemany(
        "INSERT INTO runs(project,agent,status,summary,started_at,ended_at) VALUES(?,?,?,?,?,?)",
        runs)
    conn.commit()


class TestRunsView(unittest.TestCase):
    """#4 — the RUNS history view (key `r`): every completed run, incl. task-LESS ones (a lead
    planning pass) that otherwise only flash by in the FEED. Full-body, scrollable, newest-first."""

    def _papp(self, runs=()):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-runs-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn()
        if runs:
            _seed_runs(conn, runs)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def test_load_runs_newest_first_includes_taskless(self):
        conn = _conn()
        _seed_runs(conn, [
            ("acme", "engineer", "succeeded", "cou-1: built X",
             "2026-06-30 09:00:00", "2026-06-30 09:08:00"),
            ("acme", "lead", "succeeded", "re-ranked backlog",   # task-less planning run
             "2026-06-30 10:00:00", "2026-06-30 10:03:00"),
        ])
        runs = d.load_runs(conn)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].agent, "acme/lead")      # newest first
        self.assertEqual(runs[0].summary, "re-ranked backlog")  # the task-less run is present
        self.assertEqual(runs[0].dur_min, 3)
        self.assertEqual(runs[1].agent, "acme/engineer")

    def test_load_runs_respects_limit(self):
        conn = _conn()
        _seed_runs(conn, [("p", "a", "succeeded", f"r{i}",
                           "2026-06-30 09:00:00", "2026-06-30 09:01:00") for i in range(10)])
        self.assertEqual(len(d.load_runs(conn, limit=3)), 3)

    def test_r_opens_runs_view_and_loads(self):
        papp = self._papp([("beacon", "qa", "succeeded", "lyr-2: verified",
                            "2026-06-30 09:00:00", "2026-06-30 09:02:00")])
        self.assertFalse(papp.show_runs)
        papp.handle(ord("r"), papp.left_rows(), 0, None)
        self.assertTrue(papp.show_runs)
        self.assertEqual(len(papp._runs), 1)
        self.assertEqual(papp.runs_scroll, 0)

    def test_runs_view_renders_runs(self):
        papp = self._papp([("beacon", "qa", "succeeded", "lyr-2: verified the redesign",
                            "2026-06-30 09:00:00", "2026-06-30 09:02:00")])
        papp.handle(ord("r"), papp.left_rows(), 0, None)
        scr = FakeScr(40, 200)
        pn.render_runs(scr, pn.Rect(1, 0, 30, 200), papp)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("RUNS · 1", text)
        self.assertIn("beacon/qa", text)
        self.assertIn("succeeded", text)
        self.assertIn("verified the redesign", text)

    def test_runs_view_empty_state(self):
        papp = self._papp()
        papp.handle(ord("r"), papp.left_rows(), 0, None)
        scr = FakeScr(40, 200)
        pn.render_runs(scr, pn.Rect(1, 0, 30, 200), papp)
        self.assertIn("no runs yet", "\n".join(c[2] for c in scr.calls))

    def test_runs_select_jk_and_close(self):
        papp = self._papp([("p", "a", "succeeded", f"r{i}",
                            "2026-06-30 09:00:00", "2026-06-30 09:01:00") for i in range(5)])
        papp.handle(ord("r"), papp.left_rows(), 0, None)
        papp.handle(ord("j"), [], 0, None)         # j moves the SELECTION (not raw scroll)
        self.assertEqual(papp.runs_sel, 1)
        papp.handle(ord("k"), [], 0, None)
        papp.handle(ord("k"), [], 0, None)         # clamps at 0
        self.assertEqual(papp.runs_sel, 0)
        papp.handle(27, [], 0, None)               # esc returns to the panel
        self.assertFalse(papp.show_runs)

    def test_l_routes_to_open_run_log(self):
        papp = self._papp([("cedar", "deploy", "succeeded", "deployed abc",
                            "2026-06-30 09:00:00", "2026-06-30 09:05:00")])
        papp.handle(ord("r"), papp.left_rows(), 0, None)   # open the runs view
        opened = {}
        papp._open_run_log = lambda: opened.setdefault("hit", True)
        papp.handle(ord("l"), [], 0, None)         # l opens the selected run's log
        self.assertTrue(opened.get("hit"))

    def test_open_run_log_without_file_flashes(self):
        papp = self._papp([("cedar", "deploy", "failed", "deploy failed",
                            "2026-06-30 09:00:00", "2026-06-30 09:05:00")])
        papp.handle(ord("r"), papp.left_rows(), 0, None)
        papp._runs[0].log_path = None
        papp.runs_sel = 0
        papp._open_run_log()
        self.assertIn("no log", papp.flash)

    def test_r_in_runs_view_closes(self):
        papp = self._papp([("p", "a", "succeeded", "r0",
                            "2026-06-30 09:00:00", "2026-06-30 09:01:00")])
        papp.handle(ord("r"), papp.left_rows(), 0, None)
        papp.handle(ord("r"), [], 0, None)         # r toggles back out
        self.assertFalse(papp.show_runs)


class TestRailTable(unittest.TestCase):
    """#5 — the rail is a per-project table (running · needs-you · queued · backlog) with an ALL row
    that totals across projects, so the founder sees the spread, not one filtered project at a time."""

    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-tbl-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def test_header_and_columns_render_at_real_width(self):
        papp = self._papp([("a-1", "alpha", "x", "needs_review", "high", None),
                           ("a-2", "alpha", "y", "ready", "med", None),
                           ("b-1", "bravo", "z", "backlog", "low", None)])
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 50), papp, focused=False)   # 50 cols -> all columns
        text = "\n".join(c[2] for c in scr.calls)
        for head in ("run", "you", "que", "wait", "done"):
            self.assertIn(head, text)

    def test_all_row_totals_across_projects(self):
        # band_of roll-up: proposal_review→you, ready+qa_review→que, approved→wait, done→done.
        papp = self._papp([("a-1", "alpha", "x", "proposal_review", "high", None),
                           ("a-2", "alpha", "y", "ready", "med", None),
                           ("a-3", "alpha", "w", "qa_review", "med", None),
                           ("b-1", "bravo", "z", "approved", "low", None)])
        self.assertEqual(pn._rail_counts(papp, "alpha"), (0, 1, 2, 0, 0))   # run, you, que, wait, done
        self.assertEqual(pn._rail_counts(papp, "bravo"), (0, 0, 0, 1, 0))
        self.assertEqual(pn._rail_counts(papp, "ALL"),   (0, 1, 2, 1, 0))   # totals add up

    def test_narrow_rail_sheds_columns_keeps_name(self):
        papp = self._papp([("c-1", "acme", "x", "ready", "high", None)])
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 20), papp, focused=False)   # tight: name must survive
        self.assertIn("acme", "\n".join(c[2] for c in scr.calls))


class TestDependenciesPanel(unittest.TestCase):
    """#6-B task dependencies: load_snapshot computes `blocked`, and the WORK list marks a blocked
    task with ⛓ + dims it (it won't be picked up until its predecessor is done)."""

    def _papp_with(self, inserts):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-dep-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn()
        for sql_args in inserts:
            conn.execute("INSERT INTO tasks(id,project,title,status,priority,blocked_on) "
                         "VALUES(?,?,?,?,?,?)", sql_args)
        conn.commit()
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def _task(self, papp, tid):
        for p in papp.snap.projects:
            for ts in p.tasks_by_status.values():
                for t in ts:
                    if t.id == tid:
                        return t
        return None

    def test_blocked_computed_when_predecessor_unfinished(self):
        papp = self._papp_with([("a", "p", "A", "ready", "medium", "b"),
                                ("b", "p", "B", "ready", "medium", None)])
        self.assertTrue(self._task(papp, "a").blocked)      # b is ready (not done) → a is blocked
        self.assertFalse(self._task(papp, "b").blocked)

    def test_not_blocked_when_predecessor_done(self):
        papp = self._papp_with([("a", "p", "A", "ready", "medium", "b"),
                                ("b", "p", "B", "done", "medium", None)])
        self.assertFalse(self._task(papp, "a").blocked)     # b done → a unblocked

    def test_dangling_dependency_not_blocked(self):
        papp = self._papp_with([("a", "p", "A", "ready", "medium", "ghost")])
        self.assertFalse(self._task(papp, "a").blocked)     # missing predecessor → not blocked

    def test_cross_project_dependency(self):
        papp = self._papp_with([("a", "p", "A", "ready", "medium", "z"),
                                ("z", "q", "Z", "ready", "medium", None)])
        self.assertTrue(self._task(papp, "a").blocked)      # predecessor in another project still counts

    def test_work_row_marks_blocked_and_dims(self):
        papp = self._papp_with([("a", "p", "A-thing", "ready", "high", "b"),
                                ("b", "p", "B", "ready", "medium", None)])
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 200), papp, focused=False)   # unfocused: no cursor highlight
        chained = [c for c in scr.calls if "⛓" in c[2] and "A-thing" in c[2]]
        self.assertTrue(chained)                            # the blocked task shows the ⛓ marker
        self.assertTrue(chained[0][3] & curses.A_DIM)       # ...and is dimmed (won't run yet)


class TestLiveLogColoring(unittest.TestCase):
    """The live log (inspector + log wall) colors lines by their fmt-stream marker — 💬 cyan, 🔧
    yellow, ✓ green, errors red — not just reddening errors. Regression guard for the richer view."""

    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-llc-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("w-1", "cedar", "build", "ready", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000          # color pairs identifiable by value
        return papp

    def _logfile(self):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(prefix="dais-log-"), "a.log")
        with open(p, "w") as fh:
            fh.write("💬 thinking about the fix\n🔧 Edit foo.py\n✓ done\n")
        return p

    def test_inspector_live_log_colors_by_marker(self):
        papp = self._papp()
        logf = self._logfile()
        thread = {"project": "cedar", "task": "w-1", "agent": "engineer",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": logf}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            papp.sel_id = next(r for r in papp.left_rows() if r["kind"] == "running")["id"]
            scr = FakeScr(40, 200)
            pn.render_inspector(scr, pn.Rect(1, 0, 30, 120), papp, focused=False)
        tool = next(c for c in scr.calls if "🔧" in c[2])
        chat = next(c for c in scr.calls if "💬" in c[2])
        self.assertEqual(tool[3], 4000)                       # 🔧 tool call → yellow (_cp 4)
        self.assertEqual(chat[3], 3000)                       # 💬 narration → cyan (_cp 3)


class TestRunningInspectorNotes(unittest.TestCase):
    """While a task runs, the inspector shows the task's NOTES/spec above the live log — so you can
    see what the agent is working from, not only the streaming log."""

    def test_running_inspector_shows_notes_and_log(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-rin-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn()
        conn.execute("INSERT INTO tasks(id,project,title,status,priority,notes) "
                     "VALUES('w-1','cedar','build the icon','doing','high',"
                     "'SPEC: add the launcher icon and the splash asset')")
        conn.commit()
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        logf = os.path.join(tempfile.mkdtemp(prefix="dais-log-"), "a.log")
        with open(logf, "w") as fh:
            fh.write("🔧 Edit icon.png\n")
        thread = {"project": "cedar", "task": "w-1", "agent": "engineer",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": logf}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            papp.sel_id = next(r for r in papp.left_rows() if r["kind"] == "running")["id"]
            scr = FakeScr(40, 200)
            pn.render_inspector(scr, pn.Rect(1, 0, 38, 120), papp, focused=False)
            text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("notes:", text)                                  # the spec header
        self.assertIn("add the launcher icon and the splash asset", text)   # the notes content
        self.assertIn("🔧", text)                                       # the live log still streams
        self.assertIn("live log", text)


class TestRunningTaskGuess(unittest.TestCase):
    """running_task_id: the fallback guess for a running agent is the first task in a state the
    machine auto-dispatches (band QUEUED); parked/gate states are not guessed."""

    def _proj(self, by_status):
        import machine as MC
        return d.Project(name="p", stage_goal="", running=[("engineer", None)],
                         machine=MC.load(MC.default_machine_path()),
                         tasks_by_status={st: [d.Task(id=i, title=i, status=st, priority="medium")
                                               for i in ids] for st, ids in by_status.items()})

    def test_dispatch_state_wins_over_parked(self):
        p = self._proj({"doing": ["a"], "approved": ["s"]})   # doing dispatches; approved parks
        self.assertEqual(d.running_task_id(p), "a")

    def test_ready_is_guessed(self):
        p = self._proj({"ready": ["r"], "approved": ["s"]})
        self.assertEqual(d.running_task_id(p), "r")

    def test_proposed_is_guessed_when_only_work(self):
        p = self._proj({"proposed": ["s"]})                   # proposed dispatches the lead
        self.assertEqual(d.running_task_id(p), "s")

    def test_parked_only_yields_no_guess(self):
        p = self._proj({"approved": ["s"]})                   # only a parked phase -> no guess
        self.assertEqual(d.running_task_id(p), "")



class TestInspectorLogWrapScroll(unittest.TestCase):
    """The running inspector's live log wraps long lines (no cut-off) and scrolls — detail_scroll is
    tail-anchored: 0 follows the latest, k scrolls up into history, j returns toward the tail."""

    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-lws-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("w-1", "cedar", "build", "ready", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def _run_with_log(self, papp, body):
        import tempfile
        logf = os.path.join(tempfile.mkdtemp(prefix="dais-log-"), "a.log")
        with open(logf, "w") as fh:
            fh.write(body)
        thread = {"project": "cedar", "task": "w-1", "agent": "engineer",
                  "since": "2026-06-29 00:00:00", "secs": 10, "log_path": logf}
        return thread

    def test_long_line_wraps_no_cutoff(self):
        papp = self._papp()
        thread = self._run_with_log(papp, "🔧 START " + "x" * 150 + " ENDTOKEN\n")
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            papp.sel_id = next(r for r in papp.left_rows() if r["kind"] == "running")["id"]
            scr = FakeScr(40, 60)                        # narrow pane: the long line MUST wrap
            pn.render_inspector(scr, pn.Rect(1, 0, 38, 60), papp, focused=False)
            text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("ENDTOKEN", text)                  # the tail of the long line survived (wrapped)

    def test_scroll_up_reveals_history(self):
        papp = self._papp()
        thread = self._run_with_log(papp, "".join(f"line{i:02d}\n" for i in range(30)))
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            papp.sel_id = next(r for r in papp.left_rows() if r["kind"] == "running")["id"]
            rect = pn.Rect(1, 0, 12, 80)                 # small: only a few log rows fit
            scr0 = FakeScr(40, 80)
            pn.render_inspector(scr0, rect, papp, focused=False)   # detail_scroll=0 → tail
            tail_text = "\n".join(c[2] for c in scr0.calls)
            self.assertIn("line29", tail_text)           # newest visible
            self.assertNotIn("line00", tail_text)        # oldest off-screen
            papp.detail_scroll = 99                       # scroll all the way up (clamps to the top)
            scr1 = FakeScr(40, 80)
            pn.render_inspector(scr1, rect, papp, focused=False)
            hist_text = "\n".join(c[2] for c in scr1.calls)
            self.assertIn("line00", hist_text)           # the oldest line is now visible
            self.assertNotIn("line29", hist_text)        # ...and the tail scrolled off
            self.assertIn("↑", hist_text)                # the header flags the scroll position

    def test_k_scrolls_up_j_follows_for_running(self):
        papp = self._papp()
        thread = self._run_with_log(papp, "".join(f"l{i}\n" for i in range(40)))
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            run_row = next(r for r in papp.left_rows() if r["kind"] == "running")
            papp.sel_id = run_row["id"]
            papp.pane_focus = "inspector"
            papp.handle(ord("k"), papp.left_rows(), 0, run_row)   # up → into history
            self.assertEqual(papp.detail_scroll, 1)
            papp.handle(ord("j"), papp.left_rows(), 0, run_row)   # down → back toward the tail
            self.assertEqual(papp.detail_scroll, 0)


class TestOverlayPadding(unittest.TestCase):
    """The captured-output overlay (ship / quick actions) gets the same padding as the ? help and
    action menu: a blank row top + bottom and a 2-space left margin on every line."""

    def test_overlay_blank_rows_and_left_margin(self):
        scr = FakeScr(40, 120)
        ov = {"title": "ship win-95 — done", "lines": ["▶ shipping win-95 …", "✓ win-95 → done."]}
        pn.render_overlay(scr, 40, 120, ov)
        texts = [c[2] for c in scr.calls]
        self.assertEqual(texts[0].strip(), "")               # blank top row
        self.assertEqual(texts[-1].strip(), "")              # blank bottom row
        title = next(t for t in texts if "ship win-95" in t)
        self.assertTrue(title.startswith("  "))              # 2-space left margin
        body = next(t for t in texts if "shipping" in t)
        self.assertTrue(body.startswith("  "))               # body indented too


class TestKeyStandardization(unittest.TestCase):
    """Consistent keys: q ALWAYS quits (asks to confirm) from any overlay/view; esc/any other key
    just backs out one level. q is never overloaded to mean "close this screen"."""

    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-keys-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("z-1", "zeta", "t", "ready", "med", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._confirm = lambda *a: True              # confirm "yes" → quit
        return papp

    def test_q_quits_from_help(self):
        papp = self._papp(); papp.show_help = True
        self.assertFalse(papp.handle(ord("q"), [], 0, None))   # q → quit (handle returns False)

    def test_other_key_closes_help(self):
        papp = self._papp(); papp.show_help = True
        self.assertTrue(papp.handle(ord("j"), [], 0, None))    # any other key just closes
        self.assertFalse(papp.show_help)

    def test_q_quits_from_overlay(self):
        papp = self._papp(); papp.show_overlay = True
        self.assertFalse(papp.handle(ord("q"), [], 0, None))

    def test_q_quits_from_runs_and_logwall(self):
        for view in ("show_runs", "show_logwall"):
            papp = self._papp(); setattr(papp, view, True)
            self.assertFalse(papp.handle(ord("q"), [], 0, None), view)




class TestPanelHonesty(unittest.TestCase):
    """The three panes must agree: a task being RUN is counted once (RUNNING), never also in
    its state's band; concurrent agents are distinct selectable rows; the inspector scroll
    clamps so `k` responds immediately after over-scrolling."""

    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-hon-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.refresh()
        return papp

    def test_rail_does_not_double_count_a_running_task(self):
        papp = self._papp([("a-1", "alpha", "x", "ready", "medium", None)])
        papp.snap.projects[0].running = [("engineer", "2026-06-29 00:00:00")]
        threads = [{"project": "alpha", "agent": "engineer", "task": "a-1",
                    "since": "2026-06-29 00:00:00", "log_path": None}]
        with mock.patch.object(d, "running_threads", return_value=threads):
            run, you, que, wait, done = pn._rail_counts(papp, "alpha")
        self.assertEqual((run, que), (1, 0))     # in RUN only — not also queued

    def test_concurrent_agents_are_distinct_selectable_rows(self):
        papp = self._papp([("z-1", "zeta", "t", "ready", "medium", None)])
        threads = [{"project": "zeta", "agent": "engineer", "task": "z-1",
                    "since": "2026-06-29 00:00:00", "log_path": None},
                   {"project": "zeta", "agent": "qa", "task": None,
                    "since": "2026-06-29 00:00:00", "log_path": None}]
        with mock.patch.object(d, "running_threads", return_value=threads):
            rows = papp.left_rows()
            running = [r for r in rows if r["kind"] == "running"]
            self.assertEqual(len({r["id"] for r in running}), 2)   # unique ids
            # selection can MOVE from the first running row to the second
            papp.sel_id = running[0]["id"]
            i, _ = papp._selected(rows)
            papp.sel_id = papp._move_sel(rows, i, +1)
            self.assertEqual(papp.sel_id, running[1]["id"])

    def test_inspector_scroll_clamps_back(self):
        papp = self._papp([("z-1", "zeta", "t", "ready", "medium", None)])
        papp.sel_id = "z-1"
        papp.pane_focus = "inspector"
        papp.detail_scroll = 500                            # wildly over-scrolled
        scr = FakeScr(40, 200)
        pn.render_inspector(scr, pn.Rect(1, 0, 20, 60), papp, focused=True)
        rows = papp.left_rows()
        i, sel = papp._selected(rows)
        self.assertLess(papp.detail_scroll, 500)            # clamped by the draw
        before = papp.detail_scroll
        papp.handle(ord("k"), rows, i, sel)
        self.assertEqual(papp.detail_scroll, before - 1)    # k responds immediately


class TestInspectorMachineSections(unittest.TestCase):
    """The inspector explains the machine: `next:` lists the task's outgoing edges with owner +
    guards (◆ = yours), a WAITING task names its open blockers, and `links:` shows the
    composition graph (spawned_from / blocks_parent / encompasses)."""

    def _papp(self, rows, links=()):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-insp-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        conn.executemany("INSERT INTO task_links(parent_id,child_id,rel) VALUES(?,?,?)", links)
        conn.commit()
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.refresh()
        return papp

    def _detail(self, papp, tid):
        papp.sel_id = tid
        rows = papp.left_rows()
        _, sel = papp._selected(rows)
        return pn._panel_detail_lines(papp, sel)

    def test_next_lists_edges_with_owner_and_guards(self):
        papp = self._papp([("a-1", "alpha", "t", "proposal_review", "medium", None)])
        lines = self._detail(papp, "a-1")
        self.assertIn("next:", lines)
        joined = "\n".join(lines)
        self.assertIn("◆ approve → done   (you · confirm)", joined)      # founder edge, guard shown
        self.assertIn("◆ request_changes → proposed   (you)", joined)

    def test_agent_edge_names_the_role(self):
        papp = self._papp([("a-1", "alpha", "t", "ready", "medium", None)])
        joined = "\n".join(self._detail(papp, "a-1"))
        self.assertIn("claim → doing   (engineer)", joined)

    def test_waiting_task_names_its_open_blockers(self):
        papp = self._papp([("a-1", "alpha", "t", "blocked", "medium", None),
                           ("a-2", "alpha", "fix", "ready", "medium", None)],
                          links=[("a-1", "a-2", "blocks_parent")])
        joined = "\n".join(self._detail(papp, "a-1"))
        self.assertIn("⚙ unblocked → qa_review   (system · unblocked)", joined)
        self.assertIn("waiting on a-2", joined)

    def test_terminal_task_has_no_next_section(self):
        papp = self._papp([("a-1", "alpha", "t", "done", "medium", None)])
        # done tasks live in ARCHIVE (capped list) — build detail directly
        t = pn._find_task(papp.snap, "a-1")
        row = {"kind": "task", "id": "a-1", "project": "alpha", "task": t,
               "status": "done", "sel": True}
        self.assertNotIn("next:", pn._panel_detail_lines(papp, row))

    def test_links_show_composition_both_ways(self):
        papp = self._papp([("a-1", "alpha", "parent", "blocked", "medium", None),
                           ("a-2", "alpha", "fix", "ready", "medium", None),
                           ("a-3", "alpha", "rel", "release_review", "medium", None),
                           ("a-4", "alpha", "feat", "approved", "medium", None)],
                          links=[("a-1", "a-2", "blocks_parent"),
                                 ("a-3", "a-4", "encompasses")])
        parent = "\n".join(self._detail(papp, "a-1"))
        self.assertIn("links:", parent)
        self.assertIn("⛔ blocked by a-2 (ready)", parent)     # child + its live status
        fix = "\n".join(self._detail(papp, "a-2"))
        self.assertIn("↑ fix for a-1", fix)                    # the child sees its parent
        release = "\n".join(self._detail(papp, "a-3"))
        self.assertIn("⊞ encompasses a-4", release)            # what a greenlight ships

    def test_no_links_section_when_task_is_unlinked(self):
        papp = self._papp([("a-1", "alpha", "t", "ready", "medium", None)])
        self.assertNotIn("links:", self._detail(papp, "a-1"))


class TestEmptyPhaseCollapse(unittest.TestCase):
    """A 14-state machine must not cost 14 band bars: empty phases collapse to one dim info
    line by default; `g` (expanded) shows the full flow."""

    def _papp(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-clps-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.refresh()
        return papp

    def test_empty_phases_hidden_by_default(self):
        papp = self._papp([("a-1", "alpha", "t", "ready", "medium", None)])
        rows = papp.left_rows()
        bands = [r["label"] for r in rows if r["kind"] == "band"]
        self.assertIn("READY · 1", bands)
        self.assertNotIn("PROPOSAL REVIEW  ◆ you · 0", bands)   # empty gate hidden too
        info = [r["label"] for r in rows if r["kind"] == "info"]
        self.assertTrue(any("empty phase" in ln and "g shows" in ln for ln in info))

    def test_expanded_shows_the_full_flow(self):
        papp = self._papp([("a-1", "alpha", "t", "ready", "medium", None)])
        papp._panel_expanded = True
        bands = [r["label"] for r in papp.left_rows() if r["kind"] == "band"]
        self.assertIn("PROPOSAL REVIEW  ◆ you · 0", bands)      # every phase, incl. empty gates
        self.assertIn("ARCHIVE · 0", bands)

    def test_running_band_always_present(self):
        papp = self._papp([("a-1", "alpha", "t", "ready", "medium", None)])
        bands = [r["label"] for r in papp.left_rows() if r["kind"] == "band"]
        self.assertIn("RUNNING · 0", bands)

    def test_bands_sort_by_priority_across_projects(self):
        # ALL view: bravo's critical must outrank alpha's low INSIDE the shared band
        papp = self._papp([("a-1", "alpha", "t", "ready", "low", None),
                           ("b-1", "bravo", "t", "ready", "critical", None)])
        rows = papp.left_rows()
        ids = [r["id"] for r in rows if r["kind"] == "task"]
        self.assertLess(ids.index("b-1"), ids.index("a-1"))
