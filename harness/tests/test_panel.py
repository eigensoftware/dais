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

    def test_q_quits_after_confirm(self):
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        app._confirm = lambda *a: True            # confirm the quit
        self.assertFalse(app.handle(ord("q"), app.left_rows(), 0, None))

    def test_q_cancelled_stays_alive(self):
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        app._confirm = lambda *a: False           # decline the quit
        self.assertTrue(app.handle(ord("q"), app.left_rows(), 0, None))

    def test_b_is_a_no_op_here(self):
        # 'b' is the classic show/hide-parked toggle; the panel has its own backlog +
        # deferred bands, so it's swallowed — stays alive, never flips the inherited flag.
        app = self._app([("lyr-1", "beacon", "x", "ready", "high", None)])
        self.assertFalse(app.show_parked)
        self.assertTrue(app.handle(ord("b"), app.left_rows(), 0, None))   # alive
        self.assertFalse(app.show_parked)                                 # not toggled

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

    def test_a_on_work_selection_ships_in_panel_overlay(self):
        app = self._app([("lyr-1", "beacon", "ship", "ready_to_merge", "high",
                          "https://x/y/pull/7")])
        app.pane_focus = "work"; app._confirm = lambda *a: True
        app.sel_id = "lyr-1"
        rows = app.left_rows()
        i, row = next((i, r) for i, r in enumerate(rows)
                      if r["kind"] == "task" and r["status"] == "ready_to_merge")
        with mock.patch.object(d, "subprocess") as sub:
            sub.run.return_value = mock.Mock(returncode=0, stdout="✓ lyr-1 → done.", stderr="")
            app.handle(ord("a"), rows, i, row)
            # ship runs via captured subprocess.run (no endwin/console drop)
            sub.run.assert_called_once_with(["dais", "ship", "beacon", "7"],
                                            capture_output=True, text=True, stdin=sub.DEVNULL)
        # it ran IN the panel: an overlay is up carrying the captured output
        self.assertTrue(app.show_overlay)
        self.assertTrue(any("done" in ln for ln in app._overlay["lines"]))
        # any key dismisses the overlay (modal, like help)
        app.handle(ord(" "), app.left_rows(), 0, None)
        self.assertFalse(app.show_overlay)

    def test_ship_failure_surfaces_in_overlay_and_flash(self):
        app = self._app([("lyr-1", "beacon", "ship", "ready_to_merge", "high",
                          "https://x/y/pull/7")])
        app.pane_focus = "work"; app._confirm = lambda *a: True
        app.sel_id = "lyr-1"
        rows = app.left_rows()
        i, row = next((i, r) for i, r in enumerate(rows)
                      if r["kind"] == "task" and r["status"] == "ready_to_merge")
        with mock.patch.object(d, "subprocess") as sub:
            sub.run.return_value = mock.Mock(returncode=1, stdout="", stderr="✗ merge failed")
            app.handle(ord("a"), rows, i, row)
        self.assertTrue(app.show_overlay)
        ov = "\n".join(app._overlay["lines"]) + " " + app._overlay["title"]
        self.assertIn("FAILED", ov)              # the verdict surfaces the failure
        self.assertIn("merge failed", ov)        # ... and the captured stderr
        self.assertIn("NOT merged", app.flash)   # do_action's failure flash (rc != 0)

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
        self.assertTrue(any("QUEUED" in b for b in bands), bands)
        tags = {r["tag"] for r in rows if r["kind"] == "task"}
        self.assertIn("MERGE", tags)
        self.assertIn("REVIEW", tags)
        # the loop is shown by default: loop tasks ARE their own rows (no g needed)
        self.assertTrue(any(r["kind"] == "task" and r["status"] == "ready" for r in rows))

    def test_empty_bands_collapse_to_a_single_dim_header(self):
        # a quiet band is ONE dim header (tagged empty), not a header + "(none)" filler row
        snap = self._snap(self._proj("x"))                  # nothing running / gated / in flight
        rows = pn.panel_work_rows(snap)
        for name in ("RUNNING", "NEEDS YOU", "QUEUED"):
            band = next(r for r in rows if r["kind"] == "band" and name in r["label"])
            self.assertTrue(band.get("empty"), name)        # flagged so render can dim it
        # the old two-row filler is gone
        self.assertFalse(any(r["kind"] == "info" and
                             any(s in r.get("label", "")
                                 for s in ("none running", "nothing needs you", "nothing in flight"))
                             for r in rows))

    def test_populated_band_is_not_flagged_empty(self):
        snap = self._snap(self._proj("x", ready_to_merge=1))   # NEEDS YOU has a gate
        rows = pn.panel_work_rows(snap)
        gate = next(r for r in rows if r["kind"] == "band" and "NEEDS YOU" in r["label"])
        self.assertFalse(gate.get("empty"))

    def test_unknown_status_is_auto_surfaced_as_its_own_band(self):
        # a custom status with no hardcoded band (e.g. needs_design) still shows — no panel change
        snap = self._snap(self._proj("x", needs_design=2))
        rows = pn.panel_work_rows(snap)
        band = next((r for r in rows if r["kind"] == "band" and "NEEDS DESIGN" in r["label"]), None)
        self.assertIsNotNone(band, "custom status not surfaced as a band")
        self.assertIn("NEEDS DESIGN · 2", band["label"])
        self.assertEqual(sum(1 for r in rows if r["kind"] == "task"
                             and r["status"] == "needs_design"), 2)

    def test_scoping_band_shows_needs_scoping_tasks(self):
        # a task handed to the lead (needs_scoping) appears in its own SCOPING band, tagged SCOPE
        snap = self._snap(self._proj("x", needs_scoping=2))
        rows = pn.panel_work_rows(snap)
        band = next(r for r in rows if r["kind"] == "band" and "SCOPING" in r["label"])
        self.assertIn("SCOPING · 2", band["label"])
        self.assertEqual(sum(1 for r in rows if r["kind"] == "task" and r["tag"] == "SCOPE"), 2)
        # and they are NOT mixed into QUEUED or BACKLOG
        self.assertFalse(any(r["kind"] == "task" and r["status"] == "needs_scoping"
                             and r["tag"] != "SCOPE" for r in rows))

    def test_archive_is_newest_completed_first(self):
        p = d.Project(name="x", stage_goal="", running=[], recent_runs=[],
                      tasks_by_status={"done": [
                          d.Task("x-1", "old", "done", "medium", updated_at="2026-06-01 00:00:00"),
                          d.Task("x-2", "new", "done", "medium", updated_at="2026-06-30 00:00:00")]})
        rows = pn.panel_work_rows(self._snap(p), project="x", expanded=True)
        arch = [r["id"] for r in rows if r["kind"] == "task" and r.get("tag") in ("DONE", "CANC")]
        self.assertEqual(arch, ["x-2", "x-1"])      # newest-completed first

    def test_g_uncaps_the_archive(self):
        # project picked, not expanded: archive capped + "+N older"; expanded (g): ALL archive rows
        snap = self._snap(self._proj("z", done=pn._ARCHIVE_CAP + 5))
        capped = pn.panel_work_rows(snap, project="z")
        n_capped = sum(1 for r in capped if r["kind"] == "task" and r["status"] == "done")
        self.assertEqual(n_capped, pn._ARCHIVE_CAP)
        self.assertTrue(any("older" in r.get("label", "") for r in capped))
        full = pn.panel_work_rows(snap, project="z", expanded=True)
        n_full = sum(1 for r in full if r["kind"] == "task" and r["status"] == "done")
        self.assertEqual(n_full, pn._ARCHIVE_CAP + 5)               # all of them, no cap
        self.assertFalse(any("older" in r.get("label", "") for r in full))

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

    def test_backlog_visible_deferred_collapsed_archive_last(self):
        snap = self._snap(self._proj("x", backlog=2, deferred=1, done=1, proposed=1))
        rows = pn.panel_work_rows(snap, project="x")        # default (not expanded)
        bands = [r["label"].rsplit(" · ", 1)[0] for r in rows if r["kind"] == "band"]
        # BACKLOG and DEFERRED are their own sections; ARCHIVE is the LAST band
        self.assertIn("BACKLOG", bands)
        self.assertIn("DEFERRED", bands)
        self.assertEqual(bands[-1], "ARCHIVE")
        self.assertLess(bands.index("BACKLOG"), bands.index("DEFERRED"))
        self.assertLess(bands.index("DEFERRED"), bands.index("ARCHIVE"))
        # backlog rows are visible by default; deferred rows stay collapsed until g
        self.assertTrue(any(r["kind"] == "task" and r["status"] == "backlog" for r in rows))
        self.assertFalse(any(r["kind"] == "task" and r["status"] == "deferred" for r in rows))
        self.assertFalse(any("PARKED" in r.get("label", "") for r in rows))   # old band is gone

    def test_g_expands_deferred_rows(self):
        snap = self._snap(self._proj("x", deferred=2))
        rows = pn.panel_work_rows(snap, expanded=True)
        self.assertTrue(any(r["kind"] == "task" and r["status"] == "deferred" for r in rows))

    def test_backlog_caps_then_g_shows_all(self):
        snap = self._snap(self._proj("x", backlog=pn._BACKLOG_CAP + 4))
        capped = pn.panel_work_rows(snap)                   # default: capped + "+N more"
        n_capped = sum(1 for r in capped if r["kind"] == "task" and r["status"] == "backlog")
        self.assertEqual(n_capped, pn._BACKLOG_CAP)
        self.assertTrue(any("more" in r.get("label", "") for r in capped))
        full = pn.panel_work_rows(snap, expanded=True)      # g shows all backlog
        n_full = sum(1 for r in full if r["kind"] == "task" and r["status"] == "backlog")
        self.assertEqual(n_full, pn._BACKLOG_CAP + 4)


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
    def test_inspector_is_a_wide_right_column(self):
        L = pn.layout(200, 50)
        # the inspector is a generous right column (the founder wanted it wide)
        self.assertGreaterEqual(L["inspector"].w, 80)

    def test_wide_screen_caps_left_column(self):
        # on a very wide screen the left column stops sprawling; the inspector absorbs the extra
        L = pn.layout(213, 64, n_rail_items=6)
        _covers_no_overlap(L, 213, 64)
        self.assertLessEqual(L["work"].w, pn._LEFT_MAX_W)     # left column no longer unnecessarily wide
        self.assertLessEqual(L["rail"].w, pn._LEFT_MAX_W)
        self.assertEqual(L["inspector"].x, L["work"].w)       # inspector starts where the left col ends
        self.assertGreater(L["inspector"].w, L["work"].w)     # ... and is the wider pane on a 213 screen

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
        return self._papp([("cou-5a", "acme", "review", "needs_review", "high", None),
                           ("cou-1", "acme", "done1", "done", "med", None),
                           ("cou-2", "acme", "cancelled1", "cancelled", "med", None)],
                          project="acme")

    def test_active_bands_share_one_structure_color(self):
        # consistency: every NON-EMPTY band header is the SAME cyan structure bar (not a per-band
        # hue); empty bands recede to dim (the screen reads calm when nominal)
        papp = self._papp([("g-1", "p", "gate", "needs_review", "high", None),   # NEEDS YOU active
                           ("l-1", "p", "loop", "ready", "med", None)])          # QUEUED active
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=True)
        cyan = 3000 | curses.A_REVERSE | curses.A_BOLD                # _cp(3) = structure
        self.assertEqual(self._band_attr(scr, "NEEDS YOU"), cyan)     # two active bands, one color
        self.assertEqual(self._band_attr(scr, "QUEUED"), cyan)
        self.assertEqual(self._band_attr(scr, "RUNNING"), curses.A_DIM)  # empty -> recedes (nominal)

    def test_archive_rows_are_dim(self):
        papp = self._board()
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=False)
        self.assertEqual(self._row_attr(scr, "cou-1"), curses.A_DIM)

    def test_gate_row_is_needs_you_yellow(self):
        # a founder-gate row (needs_review ∈ GATE_ORDER) carries the needs-you role, not a status hue
        papp = self._board()
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 100), papp, focused=False)  # no selection highlight
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
            papp.sel_id = "run::zeta"
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
        papp = self._papp([("cou-5a", "acme", "review", "needs_review", "high", None)],
                          project="acme")
        papp.sel_id = "cou-5a"
        scr = FakeScr(40, 200)
        pn.render_inspector(scr, pn.Rect(0, 0, 20, 60), papp, focused=False)
        head = next(c for c in scr.calls if c[2].strip().startswith("cou-5a"))
        self.assertEqual(head[3], 3000 | curses.A_BOLD)                 # _cp(3) = structure

    def test_vitals_need_you_is_yellow_when_positive(self):
        papp = self._papp([("g-1", "g", "t", "ready_to_merge", "high", "https://x/pull/1")])
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

    def test_loop_band_count_and_rows_exclude_running_task(self):   # M-NEW-1
        # one needs_qa task running, one not: the QUEUED band header excludes the running one
        # (count = 1) AND the loop rows show only the non-running task (no double-count).
        papp = self._papp([("w-9", "cedar", "qa", "needs_qa", "high", None),
                           ("w-8", "cedar", "qa2", "needs_qa", "high", None)])
        thread = {"project": "cedar", "task": "w-9", "agent": "qa",
                  "since": "2026-06-29 00:00:00", "secs": 5, "log_path": "/tmp/x.log"}
        with mock.patch.object(d, "running_threads", return_value=[thread]):
            rows = papp.left_rows()
        band = next(r for r in rows if r["kind"] == "band" and "QUEUED" in r["label"])
        self.assertIn("QUEUED · 1", band["label"])   # header excludes the running task
        loop_ids = [r.get("id") for r in rows if r["kind"] == "task" and r.get("status") == "needs_qa"]
        self.assertEqual(loop_ids, ["w-8"])            # only the non-running loop task is a row

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


class TestRailNeedsYouChip(unittest.TestCase):
    def _papp(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-rail-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, [("a-1", "alpha", "rev", "needs_review", "high", None)])
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=conn)
        papp.snap = d.load_snapshot(conn, root=root)
        papp._cp = lambda n: n * 1000
        return papp

    def test_needs_you_count_is_bold_yellow_and_separate(self):
        papp = self._papp()
        scr = FakeScr(40, 80)
        pn.render_rail(scr, pn.Rect(1, 0, 20, 24), papp, focused=False)
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
        items = pn._rail_items(papp)             # "ALL" + 15 projects = 16 rail items
        papp._rail_i = len(items) - 1            # select the very last project
        scr = FakeScr(40, 200)
        pn.render_rail(scr, pn.Rect(1, 0, 6, 30), papp, focused=True)   # body_h = 4 (title+header)
        text = "\n".join(c[2] for c in scr.calls)
        self.assertIn(items[-1], text)           # the tail project is now on-screen (was truncated)
        self.assertNotIn("ALL", text)            # the top scrolled off to make room
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
        papp.snap.projects[0].running = True
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

    def test_runs_scroll_jk_and_close(self):
        papp = self._papp([("p", "a", "succeeded", f"r{i}",
                            "2026-06-30 09:00:00", "2026-06-30 09:01:00") for i in range(5)])
        papp.handle(ord("r"), papp.left_rows(), 0, None)
        papp.handle(ord("j"), [], 0, None)
        self.assertEqual(papp.runs_scroll, 1)
        papp.handle(ord("k"), [], 0, None)
        papp.handle(ord("k"), [], 0, None)         # clamps at 0
        self.assertEqual(papp.runs_scroll, 0)
        papp.handle(27, [], 0, None)               # esc returns to the panel
        self.assertFalse(papp.show_runs)

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
        for head in ("run", "you", "scp", "que", "bkl"):
            self.assertIn(head, text)

    def test_all_row_totals_across_projects(self):
        # alpha: needs_review (you) + ready (que) + needs_scoping (scp); bravo: backlog (bkl).
        papp = self._papp([("a-1", "alpha", "x", "needs_review", "high", None),
                           ("a-2", "alpha", "y", "ready", "med", None),
                           ("a-3", "alpha", "w", "needs_scoping", "med", None),
                           ("b-1", "bravo", "z", "backlog", "low", None)])
        self.assertEqual(pn._rail_counts(papp, "alpha"), (0, 1, 1, 1, 0))   # run, you, scp, que, bkl
        self.assertEqual(pn._rail_counts(papp, "bravo"), (0, 0, 0, 0, 1))
        self.assertEqual(pn._rail_counts(papp, "ALL"), (0, 1, 1, 1, 1))     # totals

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
    """running_task_id: an engineer run (doing/ready present) is never mislabeled as the lead's
    needs_scoping task; the lead's scoping run (only needs_scoping) still shows its task."""

    def _proj(self, by_status):
        return d.Project(name="p", stage_goal="", running=[("engineer", None)],
                         tasks_by_status={st: [d.Task(id=i, title=i, status=st, priority="medium")
                                               for i in ids] for st, ids in by_status.items()})

    def test_doing_wins_over_scoping(self):
        p = self._proj({"doing": ["a"], "needs_scoping": ["s"]})
        self.assertEqual(d.running_task_id(p), "a")

    def test_ready_preferred_over_scoping(self):
        # the screenshot bug: ready work present + a needs_scoping task → show the ready task, NOT
        # the scoping one (an engineer is the one running, not the lead).
        p = self._proj({"ready": ["r"], "needs_scoping": ["s"]})
        self.assertEqual(d.running_task_id(p), "r")

    def test_scoping_shows_when_it_is_the_only_work(self):
        # by precedence the lead only runs when build/QA queues are empty → needs_scoping is the task.
        p = self._proj({"needs_scoping": ["s"]})
        self.assertEqual(d.running_task_id(p), "s")

    def test_agent_aware_engineer_skips_qa_and_scoping(self):
        # the win-95 bug: engineer running with a needs_qa task present must show its own ready task,
        # not QA's needs_qa task. With the role's handled statuses, the guess is agent-aware.
        p = self._proj({"needs_qa": ["win-95"], "ready": ["win-110"], "needs_scoping": ["win-1"]})
        self.assertEqual(d.running_task_id(p, ["changes_requested", "ready"]), "win-110")  # engineer
        self.assertEqual(d.running_task_id(p, ["needs_qa"]), "win-95")                      # qa
        self.assertEqual(d.running_task_id(p, ["needs_scoping"]), "win-1")                  # lead

    def test_agent_handles_reads_roles_file(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-ah-")
        os.makedirs(os.path.join(root, "projects", "p"))
        with open(os.path.join(root, "projects", "p", "roles"), "w") as f:
            f.write("qa review reactive needs_qa 1\nengineer edit reactive changes_requested,ready 2\n")
        self.assertEqual(d._agent_handles(root, "p", "engineer"), ["changes_requested", "ready"])
        self.assertEqual(d._agent_handles(root, "p", "qa"), ["needs_qa"])
        self.assertIsNone(d._agent_handles(root, "p", "ghost"))         # not a role → None (task-less)


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


class TestTagAlignment(unittest.TestCase):
    """Every WORK-row tag fits the {tag:<7} column, so longer statuses (needs_qa, changes_requested)
    don't shove the id/project/title columns out of alignment."""

    def _snap(self, rows):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-tag-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        conn = _conn(); _seed(conn, rows)
        return d.load_snapshot(conn, root=root)

    def test_loop_statuses_get_short_tags(self):
        self.assertEqual(pn._short_tag("needs_qa"), "QA")
        self.assertEqual(pn._short_tag("changes_requested"), "CHANGES")
        self.assertEqual(pn._short_tag("ready"), "READY")
        self.assertEqual(pn._short_tag("ready_to_merge"), "MERGE")

    def test_custom_status_abbreviated_to_fit(self):
        self.assertLessEqual(len(pn._short_tag("needs_design_review")), 7)

    def test_all_task_row_tags_fit_the_column(self):
        snap = self._snap([("w-1", "cedar", "a", "needs_qa", "high", None),
                           ("w-2", "cedar", "b", "changes_requested", "high", None),
                           ("w-3", "cedar", "c", "ready", "medium", None),
                           ("w-4", "cedar", "d", "ready_to_merge", "high", "https://x/pull/1"),
                           ("w-5", "cedar", "e", "backlog", "low", None)])
        rows = pn.panel_work_rows(snap, expanded=True)
        for r in rows:
            if r["kind"] == "task":
                self.assertLessEqual(len(r["tag"]), 7, (r["id"], r["tag"]))

    def test_render_columns_align_across_mixed_tags(self):
        # the id token must start at the same x for a 'QA' row and a 'READY' row (no shove)
        snap = self._snap([("w-1", "cedar", "alpha", "needs_qa", "high", None),
                           ("w-2", "cedar", "beta", "ready", "high", None)])
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-tag2-"); os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=_conn())
        papp.snap = snap
        scr = FakeScr(40, 200)
        pn.render_work(scr, pn.Rect(1, 0, 30, 200), papp, focused=False)
        lines = [c[2] for c in scr.calls if "cedar" in c[2]]
        self.assertGreaterEqual(len(lines), 2)
        cols = {ln.index("cedar") for ln in lines}
        self.assertEqual(len(cols), 1)                  # the project column lines up across both tags


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


class TestDeployVitals(unittest.TestCase):
    """The vitals strip shows a yellow ⬆ N DEPLOY token when projects have merged-but-undeployed work."""

    def _papp(self, pending):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-dep-")
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        papp = pn.PanelApp(FakeScr(40, 200), root=root, conn=_conn())
        projs = [d.Project(name="cedar", stage_goal="", deploy_configured=True,
                           deploy_pending=pending)]
        papp.snap = d.Snapshot(projects=projs, recent_runs=[], cap_state=False,
                               ts="2026-06-30 16:00:00", workspace="eigen")
        papp._cp = lambda n: n * 1000
        return papp

    def test_deploy_token_shows_when_pending(self):
        papp = self._papp(3)
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        text = "".join(c[2] for c in scr.calls)
        self.assertIn("⬆ 1 DEPLOY", text)                       # 1 project has undeployed merges
        tok = next(c for c in scr.calls if "DEPLOY" in c[2] and c[3] != (pn._VITALS * 1000 | curses.A_BOLD))
        self.assertEqual(tok[3], 4000 | curses.A_REVERSE | curses.A_BOLD)   # yellow founder-gate hue

    def test_no_token_when_nothing_pending(self):
        papp = self._papp(0)
        scr = FakeScr(40, 200)
        pn.render_vitals(scr, pn.Rect(0, 0, 1, 200), papp)
        self.assertNotIn("DEPLOY", "".join(c[2] for c in scr.calls))


class TestDeployState(unittest.TestCase):
    """deploy_state: detects a deploy: command, parses the deployed SHA from the latest succeeded
    deploy run, and reports the pending count (git is stubbed here — exercised live by the CLI)."""

    def _root_conn(self, deploy_line):
        import tempfile
        root = tempfile.mkdtemp(prefix="dais-ds-")
        os.makedirs(os.path.join(root, "projects", "app"))
        with open(os.path.join(root, "projects", "app", "project.yaml"), "w") as f:
            f.write("project: app\nrepo: /tmp/x\n%s\n" % deploy_line)
        return root, _conn()

    def test_no_deploy_configured(self):
        root, conn = self._root_conn("# none")
        self.assertEqual(d.deploy_state(root, "app", conn), (False, None, None))

    def test_configured_parses_sha_and_time(self):
        root, conn = self._root_conn("deploy: echo hi")
        conn.execute("INSERT INTO runs(project,agent,status,summary,ended_at) "
                     "VALUES('app','deploy','succeeded','deployed abc1234','2026-06-30 10:00:00')")
        conn.commit()
        with mock.patch.object(d, "_deploy_pending", return_value=5) as mp:
            configured, pending, last = d.deploy_state(root, "app", conn)
        self.assertTrue(configured)
        self.assertEqual(pending, 5)
        self.assertEqual(last, "2026-06-30 10:00:00")
        mp.assert_called_once_with("/tmp/x", "abc1234")    # SHA parsed from the run summary
