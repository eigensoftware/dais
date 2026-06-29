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


class TestBreakpoint(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(pn.breakpoint(200), "wide")
        self.assertEqual(pn.breakpoint(160), "wide")
        self.assertEqual(pn.breakpoint(159), "medium")
        self.assertEqual(pn.breakpoint(100), "medium")
        self.assertEqual(pn.breakpoint(99), "narrow")
        self.assertEqual(pn.breakpoint(80), "narrow")


class TestLayout(unittest.TestCase):
    def test_wide_has_all_columns(self):
        L = pn.layout(200, 50)
        self.assertEqual(set(L), {"vitals", "rail", "work", "inspector", "feed", "bar"})
        _covers_no_overlap(L, 200, 50)
        self.assertEqual(L["vitals"].y, 0)
        self.assertEqual(L["bar"].y, 49)
        # columns left-to-right: rail, work, inspector
        self.assertLess(L["rail"].x, L["work"].x)
        self.assertLess(L["work"].x, L["inspector"].x)

    def test_medium_drops_rail(self):
        L = pn.layout(120, 40)
        self.assertNotIn("rail", L)
        self.assertEqual(set(L), {"vitals", "work", "inspector", "feed", "bar"})
        _covers_no_overlap(L, 120, 40)

    def test_narrow_work_only(self):
        L = pn.layout(80, 24)
        self.assertNotIn("rail", L)
        self.assertNotIn("inspector", L)
        self.assertIn("work", L)
        self.assertIn("bar", L)
        _covers_no_overlap(L, 80, 24)
        # work spans the full width
        self.assertEqual(L["work"].x, 0)
        self.assertEqual(L["work"].w, 80)

    def test_short_height_drops_feed(self):
        L = pn.layout(120, 12)
        self.assertNotIn("feed", L)
        _covers_no_overlap(L, 120, 12)

    def test_toggles_force_panes_off(self):
        L = pn.layout(200, 50, show_rail=False, show_inspector=False, show_feed=False)
        self.assertEqual(set(L), {"vitals", "work", "bar"})
        _covers_no_overlap(L, 200, 50)


class TestFocus(unittest.TestCase):
    def test_focus_order_wide(self):
        L = pn.layout(200, 50)
        self.assertEqual(pn.focus_order(L), ["rail", "work", "inspector"])

    def test_focus_order_narrow_is_work_only(self):
        L = pn.layout(80, 24)
        self.assertEqual(pn.focus_order(L), ["work"])

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
from test_cockpit import FakeScr, _conn, make_app   # reuse the wired-App helpers


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
    def _app(self, rows):
        conn = _conn(); _seed(conn, rows)
        return make_app(conn=conn, h=40, w=200)

    def _app_with_notes(self, rows_with_notes):
        conn = _conn(); _seed_with_notes(conn, rows_with_notes)
        return make_app(conn=conn, h=40, w=200)

    def test_work_renders_rows_within_rect(self):
        import tempfile
        conn = _conn()
        _seed(conn, [("lyr-1", "beacon", "ship it", "ready_to_merge", "high",
                      "https://x/pull/9")])
        root = tempfile.mkdtemp(prefix="dais-pwr-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        app = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        app.snap = d.load_snapshot(conn, root=root)
        scr = FakeScr(40, 200)
        rect = pn.Rect(2, 5, 20, 60)
        pn.render_work(scr, rect, app, focused=True)
        # something drew the gate banner, and nothing escaped the rect
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("NEEDS YOU", text)
        for (y, x, s, _a) in scr.calls:
            self.assertGreaterEqual(y, rect.y)
            self.assertLess(y, rect.y + rect.h)
            self.assertGreaterEqual(x, rect.x)
            self.assertLessEqual(pn.disp_width(s), rect.x + rect.w - x)

    def test_inspector_shows_selection_detail(self):
        app = self._app([("lyr-1", "beacon", "ship it", "ready_to_merge", "high",
                          "https://x/pull/9")])
        app.sel_id = "lyr-1"
        scr = FakeScr(40, 200)
        rect = pn.Rect(2, 70, 20, 60)
        pn.render_inspector(scr, rect, app, focused=False)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("lyr-1", text)
        self.assertIn("ready_to_merge", text)
        for (y, x, s, _a) in scr.calls:
            self.assertGreaterEqual(y, rect.y)
            self.assertLess(y, rect.y + rect.h)
            self.assertGreaterEqual(x, rect.x)
            self.assertLessEqual(pn.disp_width(s), rect.x + rect.w - x)

    def test_inspector_scroll_offsets_visible_window(self):
        """I1: detail_scroll must advance the visible window in render_inspector."""
        long_notes = "  ".join(f"note-line-{i}" for i in range(30))
        app = self._app_with_notes([
            ("lyr-1", "beacon", "ship it", "ready_to_merge", "high",
             "https://x/pull/9", long_notes),
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


class TestChromePanes(unittest.TestCase):
    def _app(self, rows):
        conn = _conn(); _seed(conn, rows)
        return make_app(conn=conn, h=40, w=200)

    def test_vitals_has_workspace_and_gate_count_honestly(self):
        app = self._app([("cou-1", "acme", "approve", "proposed", "high", None)])
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), app)
        text = scr.calls[0][2]               # full header is first call; ng>0 adds a yellow overlay
        self.assertIn("DAIS", text)
        self.assertIn("1 need you", text)     # honest gate count
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
        app = self._app([("lyr-1", "beacon", "ship", "ready_to_merge", "high",
                          "https://x/pull/9")])
        app.sel_id = "lyr-1"
        scr = FakeScr(40, 200)
        pn.render_bar(scr, pn.Rect(39, 0, 1, 200), app, focus="work")
        text = scr.calls[-1][2]
        self.assertIn("ship", text)           # contextual action bar reused
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
        app = self._app([("lyr-1", "beacon", "ship", "ready_to_merge", "high",
                          "https://x/pull/9"),
                         ("win-1", "cedar", "build", "ready", "high", None)])
        app.draw()
        text = "\n".join(c[2] for c in app.scr.calls)
        self.assertIn("DAIS", text)            # vitals
        self.assertIn("NEEDS YOU", text)       # work
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

    def test_q_quits(self):
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        self.assertFalse(app.handle(ord("q"), app.left_rows(), 0, None))

    def test_a_on_work_selection_dispatches_engine(self):
        app = self._app([("lyr-1", "beacon", "ship", "ready_to_merge", "high",
                          "https://x/y/pull/7")])
        app.pane_focus = "work"; app._confirm = lambda *a: True
        app.sel_id = "lyr-1"
        rows = app.left_rows()
        i, row = next((i, r) for i, r in enumerate(rows)
                      if r["kind"] == "task" and r["status"] == "ready_to_merge")
        with mock.patch.object(d, "subprocess") as sub:
            sub.call.return_value = 0
            app.handle(ord("a"), rows, i, row)
            sub.call.assert_called_once_with(["dais", "ship", "beacon", "7"])

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


class TestPanelWorkRows(unittest.TestCase):
    def _snap(self, *projects):
        return d.Snapshot(projects=list(projects), recent_runs=[], cap_state=False,
                          ts="2026-06-29 00:00:00")

    def _proj(self, name, **by):
        tbs = {st: [d.Task(id=f"{name}-{st}-{i}", title="t", status=st, priority="medium")
                    for i in range(n)] for st, n in by.items()}
        return d.Project(name=name, stage_goal="", running=[], tasks_by_status=tbs,
                         recent_runs=[])

    def _kinds(self, rows):
        return [(r["kind"], r.get("label", r.get("id"))) for r in rows]

    def test_bands_present_with_gate_tags(self):
        snap = self._snap(self._proj("beacon", ready_to_merge=1),
                          self._proj("acme", needs_review=1, ready=2))
        rows = pn.panel_work_rows(snap)
        # a NEEDS YOU band bar, then tagged gate task rows
        bands = [r["label"] for r in rows if r["kind"] == "band"]
        self.assertTrue(any("NEEDS YOU" in b for b in bands), bands)
        self.assertTrue(any("THE LOOP" in b for b in bands), bands)
        tags = {r["tag"] for r in rows if r["kind"] == "task"}
        self.assertIn("MERGE", tags)
        self.assertIn("REVIEW", tags)
        # collapsed: loop tasks are NOT individual task rows
        self.assertFalse(any(r["kind"] == "task" and r["status"] == "ready" for r in rows))

    def test_expanded_shows_loop_rows_and_archive(self):
        snap = self._snap(self._proj("cedar", ready=2, done=3, proposed=1))
        rows = pn.panel_work_rows(snap, expanded=True)
        self.assertTrue(any(r["kind"] == "task" and r["status"] == "ready" for r in rows))
        bands = [r["label"] for r in rows if r["kind"] == "band"]
        self.assertTrue(any("ARCHIVE" in b for b in bands), bands)
        self.assertTrue(any(r["kind"] == "task" and r["status"] == "done" for r in rows))

    def test_project_filter_limits_rows(self):
        snap = self._snap(self._proj("beacon", ready_to_merge=1),
                          self._proj("acme", proposed=1))
        rows = pn.panel_work_rows(snap, project="beacon")
        projs = {r["project"] for r in rows if r["kind"] == "task"}
        self.assertEqual(projs, {"beacon"})

    def test_parked_only_when_requested(self):
        snap = self._snap(self._proj("x", backlog=1, deferred=1, proposed=1))
        self.assertFalse(any("PARKED" in r.get("label", "")
                             for r in pn.panel_work_rows(snap)))
        rows = pn.panel_work_rows(snap, show_parked=True)
        self.assertTrue(any("PARKED" in r.get("label", "") for r in rows))
        self.assertTrue(any(r["kind"] == "task" and r["status"] == "backlog" for r in rows))


class TestRenderWorkNative(unittest.TestCase):
    def _app(self, rows):
        conn = _conn(); _seed(conn, rows)
        return make_app(conn=conn, h=40, w=200)

    def test_band_bars_and_tags_render(self):
        app = self._app([("lyr-1", "beacon", "ship it", "ready_to_merge", "high",
                          "https://x/pull/9"),
                         ("cou-1", "acme", "post", "needs_review", "high", None)])
        # PanelApp model: make a PanelApp to get the override + state
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=app.conn)
        papp.snap = d.load_snapshot(app.conn, root=root)
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 90), papp, focused=True)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn("NEEDS YOU", text)
        self.assertIn("MERGE", text)
        self.assertIn("REVIEW", text)
        for (y, x, s, _a) in scr.calls:                # within the rect
            self.assertGreaterEqual(y, 1); self.assertLess(y, 31)
            self.assertLessEqual(pn.disp_width(s), 90 - x)

    def test_panelapp_left_rows_is_the_panel_model(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-pb2-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("lyr-1","beacon","x","ready_to_merge","high","https://x/pull/9")])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        rows = papp.left_rows()
        self.assertTrue(any(r["kind"] == "band" and "NEEDS YOU" in r["label"] for r in rows))
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
    def test_inspector_is_wider_than_before(self):
        L = pn.layout(200, 50)
        # inspector should be ~half of (w - rail), i.e. notably wider than the old 2/5
        self.assertGreaterEqual(L["inspector"].w, (200 - pn.RAIL_W) // 2 - 2)

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
        for k in ("tab", "b parked", "g ", "/ filter", "? help", "q quit"):
            self.assertIn(k, text)

    def test_question_mark_toggles_help_and_draw_shows_it(self):
        papp = self._papp([("lyr-1","beacon","x","ready","high",None)])
        self.assertFalse(papp.show_help)
        papp.handle(ord("?"), papp.left_rows(), 0, None)
        self.assertTrue(papp.show_help)
        papp.draw()
        text = "\n".join(c[2] for c in papp.scr.calls)
        self.assertIn("KEYS", text)                # the overlay title
        self.assertIn("tab", text)
        # any key closes
        papp.handle(ord("x"), papp.left_rows(), 0, None)
        self.assertFalse(papp.show_help)


class TestFullChromatic(unittest.TestCase):
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
        return self._papp([("cou-5a", "acme", "review", "needs_review", "high", None),
                           ("cou-1", "acme", "done1", "done", "med", None),
                           ("cou-2", "acme", "cancelled1", "cancelled", "med", None)],
                          project="acme")

    def test_bands_are_urgency_colored(self):
        papp = self._board()
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=True)
        self.assertEqual(self._band_attr(scr, "NEEDS YOU"),
                         4000 | curses.A_REVERSE | curses.A_BOLD)     # yellow
        self.assertEqual(self._band_attr(scr, "RUNNING"),
                         1000 | curses.A_REVERSE | curses.A_BOLD)     # green
        self.assertEqual(self._band_attr(scr, "THE LOOP"),
                         3000 | curses.A_REVERSE | curses.A_BOLD)     # cyan
        self.assertEqual(self._band_attr(scr, "ARCHIVE"), curses.A_DIM)  # recedes

    def test_archive_rows_are_dim(self):
        papp = self._board()
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=False)
        self.assertEqual(self._row_attr(scr, "cou-1"), curses.A_DIM)

    def test_needs_review_row_is_cyan_not_white(self):
        papp = self._board()
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=False)  # no selection highlight
        self.assertEqual(self._row_attr(scr, "cou-5a"), 3000)            # cyan, not 6000 white

    def test_selected_row_keeps_status_hue(self):
        papp = self._board()
        papp.sel_id = "cou-5a"
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=True)
        self.assertEqual(self._row_attr(scr, "cou-5a"), 3000 | curses.A_REVERSE)

    def test_inspector_attr_rules(self):
        papp = self._papp([("z-1", "z", "t", "ready", "med", None)])
        self.assertEqual(pn._inspector_attr(papp, 5, "12:59 gc      succeeded   2m"), 1000)  # green
        self.assertEqual(pn._inspector_attr(papp, 5, "07:07 lead    failed      3m"), 2000)  # red
        self.assertEqual(pn._inspector_attr(papp, 0, "notes:"), 3000 | curses.A_BOLD)        # header
        self.assertEqual(pn._inspector_attr(papp, 0, "runs touching z-1:"), 3000 | curses.A_BOLD)
        self.assertEqual(pn._inspector_attr(papp, 0,
                         "assignee founder · prio high · pr (none)"), 4000)         # yellow

    def test_vitals_need_you_is_yellow_when_positive(self):
        papp = self._papp([("g-1", "g", "t", "ready_to_merge", "high", "https://x/pull/1")])
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        hits = [c for c in scr.calls
                if "need you" in c[2] and c[3] == (4000 | curses.A_REVERSE | curses.A_BOLD)]
        self.assertTrue(hits)

    def test_vitals_no_yellow_overlay_when_zero(self):
        papp = self._papp([("g-1", "g", "t", "ready", "low", None)])   # 'ready' is loop, not a gate
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        self.assertFalse([c for c in scr.calls
                          if c[3] == (4000 | curses.A_REVERSE | curses.A_BOLD)])

    def test_rail_projects_get_distinct_hues_and_running_is_green(self):
        papp = self._papp([("a-1", "alpha", "x", "ready", "med", None),
                           ("b-1", "bravo", "y", "ready", "med", None)])
        # if the snapshot derives projects differently, adapt the seed so snap.projects has >=2 names
        names = [p.name for p in papp.snap.projects]
        self.assertGreaterEqual(len(names), 2)
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 20), papp, focused=False)
        # ALL row carries no hue; the two project rows carry different non-zero hues
        all_attr = self._row_attr_rail(scr, "ALL")
        self.assertEqual(all_attr, 0)
        a_attr = self._row_attr_rail(scr, names[0])
        b_attr = self._row_attr_rail(scr, names[1])
        self.assertNotEqual(a_attr, 0)
        self.assertNotEqual(a_attr, b_attr)
        # mark the first project running -> its row turns green (cp 1)
        papp.snap.projects[0].running = True
        scr2 = FakeScr(40, 200)
        pn.render_rail(scr2, pn.Rect(1, 0, 20, 20), papp, focused=False)
        self.assertEqual(self._row_attr_rail(scr2, names[0]) & 1000, 1000)

    @staticmethod
    def _row_attr_rail(scr, needle):
        for c in scr.calls:
            if needle in c[2]:
                return c[3]
        return None


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

    def test_default_loop_summary_has_no_emoji(self):              # I-2
        papp = self._papp([("a-1", "p", "t", "ready", "med", None)])          # default (all-projects) view
        rows = papp.left_rows()
        loop = next(r for r in rows if r.get("id") == "__loop_sum")
        self.assertNotIn("⚙", loop["label"])                  # no ⚙ gear
        self.assertNotIn("for full board", loop["label"])          # no duplicate of "(press g to expand)"
        self.assertIn("the loop", loop["label"])

    def test_running_gate_task_not_double_counted(self):           # I-3
        papp = self._papp([("cou-5a", "acme", "review", "needs_review", "high", None)])
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

    def test_loop_summary_count_matches_band_when_task_running(self):   # M-NEW-1
        # one needs_qa task running, one not: the THE LOOP band header AND the collapsed summary text
        # must BOTH exclude the running one (count = 1), or they contradict during live watch.
        papp = self._papp([("w-9", "cedar", "qa", "needs_qa", "high", None),
                           ("w-8", "cedar", "qa2", "needs_qa", "high", None)])
        thread = {"project": "cedar", "task": "w-9", "agent": "qa",
                  "since": "2026-06-29 00:00:00", "secs": 5, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
        band = next(r for r in rows if r["kind"] == "band" and "THE LOOP" in r["label"])
        summ = next(r for r in rows if r.get("id") == "__loop_sum")
        self.assertIn("THE LOOP · 1", band["label"])   # header excludes the running task
        self.assertIn("1 needs_qa", summ["label"])     # summary agrees
        self.assertNotIn("2 needs_qa", summ["label"])

    def test_inspector_running_hint_uses_lowercase_l(self):        # M-1
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
        self.assertIn("press l", text)
        self.assertNotIn("press L", text)
        self.assertNotIn("coming in the log phase", text)


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

    def test_focused_title_has_marker_and_magenta_accent(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_pane_title(scr, pn.Rect(0, 0, 10, 40), "WORK", True, papp)
        call = scr.calls[-1]
        self.assertIn("▶ WORK", call[2])
        self.assertEqual(call[3], 5000 | curses.A_REVERSE | curses.A_BOLD)   # magenta bar

    def test_unfocused_title_is_dim_without_marker(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_pane_title(scr, pn.Rect(0, 0, 10, 40), "WORK", False, papp)
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

    def test_band_header_and_live_tail_with_red_errors(self):
        import tempfile
        logf = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, prefix="dais-lwlog-")
        logf.write("starting build\nrunning tests\nTraceback (most recent call last)\n")
        logf.close()
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
