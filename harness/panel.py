#!/usr/bin/env python3
"""dais control panel -- the responsive multi-pane TUI (a view rebuild of `dais top`).

PanelApp subclasses dashboard.App: it inherits all data/action-engine/log/selection
logic and overrides only the view (draw/handle) to render panes into a responsive
layout. Reached via DAIS_PANEL=1; the classic `dais top` is unchanged.
"""
from collections import namedtuple

import curses

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
