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
    for idx, ln in enumerate(wrapped[:inner.h]):
        _add(scr, inner.y + idx, inner.x, ln, inner.x + inner.w)
