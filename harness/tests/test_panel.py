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
from test_cockpit import FakeScr, _conn, make_app   # reuse the wired-App helpers


def _seed(conn, rows):
    conn.executemany(
        "INSERT INTO tasks(id,project,title,status,priority,pr_url) VALUES(?,?,?,?,?,?)",
        rows)
    conn.commit()


class TestPaneRenderers(unittest.TestCase):
    def _app(self, rows):
        conn = _conn(); _seed(conn, rows)
        return make_app(conn=conn, h=40, w=200)

    def test_work_renders_rows_within_rect(self):
        app = self._app([("lyr-1", "beacon", "ship it", "ready_to_merge", "high",
                          "https://x/pull/9")])
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
