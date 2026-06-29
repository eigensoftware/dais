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
        text = scr.calls[-1][2]
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
        self.assertIn(": command", text)
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
