#!/usr/bin/env python3
"""dais control panel -- the responsive multi-pane TUI (a view rebuild of `dais top`).

PanelApp subclasses dashboard.App: it inherits all data/action-engine/log/selection
logic and overrides only the view (draw/handle) to render panes into a responsive
layout. Reached via DAIS_PANEL=1; the classic `dais top` is unchanged.
"""
from collections import namedtuple

import curses
import textwrap

import dashboard as d
from dashboard import _add, clip_cols, pad_cols, disp_width  # width-aware primitives

Rect = namedtuple("Rect", "y x h w")

PANES = ("vitals", "rail", "work", "inspector", "feed", "bar")
RAIL_W = 22
_MIN_FEED_H = 14          # below this terminal height, the feed is dropped


def breakpoint(w):
    if w >= 160:
        return "wide"
    if w >= 100:
        return "medium"
    return "narrow"


def layout(w, h, *, show_rail=True, show_inspector=True, show_feed=True):
    """Terminal size + toggles -> {pane_id: Rect}. Rects tile the screen, no overlap.
    Header is row 0; bar is the last row; feed (when shown and there's height) is the
    row above the bar; the middle band splits into columns by breakpoint."""
    bp = breakpoint(w)
    out = {"vitals": Rect(0, 0, 1, w), "bar": Rect(h - 1, 0, 1, w)}
    feed_on = show_feed and h >= _MIN_FEED_H
    if feed_on:
        out["feed"] = Rect(h - 2, 0, 1, w)
    mid_y = 1
    mid_h = (h - 2 if not feed_on else h - 3)     # rows between vitals and feed/bar
    mid_h = max(1, mid_h)
    rail_on = show_rail and bp == "wide"
    insp_on = show_inspector and bp in ("wide", "medium")
    x = 0
    if rail_on:
        out["rail"] = Rect(mid_y, 0, mid_h, RAIL_W)
        x = RAIL_W
    if insp_on:
        insp_w = max(30, (w - x) * 2 // 5)
        work_w = w - x - insp_w
        out["work"] = Rect(mid_y, x, mid_h, work_w)
        out["inspector"] = Rect(mid_y, x + work_w, mid_h, insp_w)
    else:
        out["work"] = Rect(mid_y, x, mid_h, w - x)
    return out


# panes that can take selection/scroll focus, in tab order (feed/vitals/bar are passive)
_FOCUS_TABS = ("rail", "work", "inspector")


def focus_order(rects):
    """Focusable pane ids present in this layout, in stable tab order."""
    return [p for p in _FOCUS_TABS if p in rects]


def cycle_focus(current, order, direction=1):
    if not order:
        return None
    if current not in order:
        return order[0]
    return order[(order.index(current) + direction) % len(order)]


def render_pane_title(scr, rect, title, focused):
    """Draw a pane's title row (reverse bar); returns the inner Rect below it."""
    attr = curses.A_REVERSE | (curses.A_BOLD if focused else 0)
    _add(scr, rect.y, rect.x, pad_cols(f" {title}", rect.w), rect.x + rect.w, attr)
    return Rect(rect.y + 1, rect.x, max(0, rect.h - 1), rect.w)


def render_work(scr, rect, app, focused):
    """The decisions/running/board list (app.left_rows) drawn into rect, scrolled to
    keep the selection visible — the same windowing App.draw uses for its left pane."""
    inner = render_pane_title(scr, rect, "WORK", focused)
    rows = app.left_rows()
    sel_i, sel_row = app._selected(rows)
    app.sel_id = sel_row["id"] if sel_row else None
    base = max(0, sel_i - inner.h + 1) if rows else 0
    for idx, r in enumerate(rows[base:base + inner.h]):
        attr = app._row_attr(r)
        if (base + idx) == sel_i and focused and r.get("sel", True):
            attr |= curses.A_REVERSE
        _add(scr, inner.y + idx, inner.x, pad_cols(r["label"], inner.w),
             inner.x + inner.w, attr)


def render_inspector(scr, rect, app, focused):
    """Detail of the current selection (app.detail_lines), wrapped to the pane width."""
    inner = render_pane_title(scr, rect, "INSPECTOR", focused)
    rows = app.left_rows()
    _, sel_row = app._selected(rows)
    wrapped = []
    for ln in app.detail_lines(sel_row):
        if disp_width(ln) <= inner.w:
            wrapped.append(ln)
        else:
            wrapped.extend(textwrap.wrap(ln, inner.w) or [""])
    start = max(0, min(app.detail_scroll, max(0, len(wrapped) - 1)))
    for idx, ln in enumerate(wrapped[start:start + inner.h]):
        _add(scr, inner.y + idx, inner.x, ln, inner.x + inner.w)


def render_vitals(scr, rect, app):
    """Honest org vitals: identity · watch state · running/idle · cooling · gates · clock.
    NO budget bar (no usage data exists)."""
    snap = app.snap
    ws = snap.workspace if snap else None
    ident = f"DAIS · {ws} · LIVE" if ws else "DAIS · LIVE"
    now = app._now()
    threads = d.running_threads(snap, now) if snap else []
    running_ids = {(t["project"], t["task"]) for t in threads if t["task"]}
    wstate, wint, wpar = d.watch_state(app.root)
    badge = (f"watch {wint or '?'}s x{wpar or '?'}" if wstate == "running"
             else "PAUSED" if wstate == "paused" else "watch stopped")
    nproj = len(snap.projects) if snap else 0
    ng = d.gate_count(snap, running_ids) if snap else 0
    cool = " · COOLING" if (snap and snap.cap_state) else ""
    clk = d.to_local_hhmm(snap.ts, with_secs=True) if snap else ""
    head = (f" {ident}  {badge} · >{len(threads)} running · {nproj} proj"
            f" · {ng} need you{cool}  {clk}")
    _add(scr, rect.y, rect.x, pad_cols(head, rect.w), rect.x + rect.w,
         curses.A_REVERSE | curses.A_BOLD)


def render_rail(scr, rect, app, focused):
    """Project navigator with running/gate chips."""
    inner = render_pane_title(scr, rect, "PROJECTS", focused)
    snap = app.snap
    if not snap:
        return
    for idx, p in enumerate(snap.projects[:inner.h]):
        run = " >" if p.running else "  "
        ng = sum(len(p.tasks_by_status.get(st, [])) for st in d.GATE_ORDER)
        chip = f" !{ng}" if ng else ""
        _add(scr, inner.y + idx, inner.x, clip_cols(f"{run} {p.name}{chip}", inner.w),
             inner.x + inner.w)


def render_feed(scr, rect, app):
    """Phase-A placeholder line; the real activity feed lands in the feed/vitals phase."""
    _add(scr, rect.y, rect.x, pad_cols(" FEED  (activity ticker — coming next phase)",
         rect.w), rect.x + rect.w, curses.A_DIM)


def render_bar(scr, rect, app, focus):
    """Contextual action bar (reused) + the panel's global keys."""
    rows = app.left_rows()
    _, sel_row = app._selected(rows)
    acts = app.action_bar(sel_row) if sel_row else ""
    if getattr(app, "filtering", False):
        hint = f" /{app.filter}_  ·  : command · tab pane · ? help · q quit"
    else:
        hint = f" {acts}  ·  : command · tab pane · ? help · q quit"
    _add(scr, rect.y, rect.x, pad_cols(hint, rect.w), rect.x + rect.w,
         curses.A_REVERSE)


_RENDER = {
    "vitals": lambda scr, r, app, foc: render_vitals(scr, r, app),
    "rail": render_rail,
    "work": render_work,
    "inspector": render_inspector,
    "feed": lambda scr, r, app, foc: render_feed(scr, r, app),
}


class PanelApp(d.App):
    """The control panel: App's data/engine/log/selection, a multi-pane responsive view."""

    def __init__(self, scr, interval=2.0, root=d.HOME, conn=None):
        super().__init__(scr, interval=interval, root=root, conn=conn)
        self.pane_focus = "work"

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        rects = layout(w, h, show_rail=True, show_inspector=True, show_feed=True)
        order = focus_order(rects)
        if self.pane_focus not in order:
            self.pane_focus = order[0] if order else "work"
        for pid, rect in rects.items():
            if pid == "bar":
                render_bar(scr, rect, self, self.pane_focus)
            else:
                _RENDER[pid](scr, rect, self, pid == self.pane_focus)
        scr.refresh()

    def handle(self, ch, rows, sel_i, sel_row):
        # filtering takes priority — all keystrokes route to the inherited filter handler
        if self.filtering:
            return super().handle(ch, rows, sel_i, sel_row)
        # global panel keys first
        if ch == ord("q"):
            return False
        if ch in (9,):                          # tab -> next focusable pane
            h, w = self.scr.getmaxyx()
            order = focus_order(layout(w, h))
            self.pane_focus = cycle_focus(self.pane_focus, order, +1) or "work"
            return True
        if ch == curses.KEY_BTAB:               # shift-tab -> previous
            h, w = self.scr.getmaxyx()
            order = focus_order(layout(w, h))
            self.pane_focus = cycle_focus(self.pane_focus, order, -1) or "work"
            return True
        # navigation/actions route to the focused pane's selection via the inherited App
        if self.pane_focus == "inspector" and ch in (ord("j"), ord("k"),
                                                      curses.KEY_DOWN, curses.KEY_UP):
            self.detail_scroll += 1 if ch in (ord("j"), curses.KEY_DOWN) else -1
            self.detail_scroll = max(0, self.detail_scroll)
            return True
        # everything else (j/k select, a/x/+/-/n/o/enter, /, b, ...) reuses App.handle,
        # which already drives selection + the action engine on `rows`/`sel_row`.
        self.focus = "left"                     # App.handle's left-pane selection path
        return super().handle(ch, rows, sel_i, sel_row)
