#!/usr/bin/env python3
"""dais control panel -- the responsive multi-pane TUI (a view rebuild of `dais top`).

PanelApp subclasses dashboard.App: it inherits all data/action-engine/log/selection
logic and overrides only the view (draw/handle) to render panes into a responsive
layout. This is the default `dais top`; `DAIS_CLASSIC=1 dais top` opts back into the
classic single-pane UI. The panel reads as a mission-control cockpit: focal-point vitals
(a distinct-coloured top readout), status dots, bands that collapse when empty, and an
outlined inspector.
"""
from collections import namedtuple

import curses
import re
import textwrap

import dashboard as d
from dashboard import _add, clip_cols, pad_cols, disp_width  # width-aware primitives

Rect = namedtuple("Rect", "y x h w")

_MIN_FEED_H = 14          # below this terminal height, the feed is dropped
_TWO_COL_MIN_W = 76       # below this width the columns stack into one (nothing is dropped)
_INSP_MIN_W = 36          # the inspector is never narrower than this in two-column mode
_INSP_PCT = 45            # inspector target width ≈ 45% of the screen at medium widths
_LEFT_MAX_W = 96          # the left column (PROJECTS+WORK) never sprawls past this; extra -> inspector
_PROJ_MAX_H = 12          # cap the PROJECTS block so WORK always keeps room
_MIN_WORK_H = 5           # ... and leave WORK at least this many rows when possible
_LEFT_GAP = 1             # blank separator row between PROJECTS and WORK in the left column


def _projects_h(n_rail_items, mid_h):
    """Height of the PROJECTS block (a title row + one row per rail item), capped so WORK keeps
    room and never taller than the middle band."""
    want = 1 + max(1, n_rail_items)
    ceiling = max(1, mid_h - _MIN_WORK_H)
    return max(1, min(want, _PROJ_MAX_H, ceiling, mid_h))


def layout(w, h, *, n_rail_items=1, show_feed=True):
    """Terminal size -> {pane_id: Rect}, tiling the screen with no overlap. The control panel is
    TWO columns that never drop a pane: the left column stacks PROJECTS over WORK, the right column
    is the INSPECTOR (full middle height). Vitals is row 0, the bar the last row, the feed (when
    there's height) the row above it. Below _TWO_COL_MIN_W the three middle panes stack into one
    full-width column (PROJECTS -> WORK -> INSPECTOR) instead of side-by-side -- nothing is hidden.
    `n_rail_items` (passed by draw as len(_rail_items)) sizes the PROJECTS block."""
    out = {"vitals": Rect(0, 0, 1, w), "bar": Rect(h - 1, 0, 1, w)}
    feed_on = show_feed and h >= _MIN_FEED_H
    if feed_on:
        out["feed"] = Rect(h - 2, 0, 1, w)
    mid_y = 1
    mid_h = max(1, (h - 3 if feed_on else h - 2))     # rows between vitals and feed/bar
    proj_h = _projects_h(n_rail_items, mid_h)
    if w >= _TWO_COL_MIN_W:
        # left column ~ (100 - _INSP_PCT)% of width but capped at _LEFT_MAX_W so it never sprawls;
        # the inspector absorbs whatever is left (long notes benefit from the room).
        left_w = min(w * (100 - _INSP_PCT) // 100, _LEFT_MAX_W)
        left_w = max(1, min(left_w, w - _INSP_MIN_W))    # keep the inspector at least _INSP_MIN_W
        insp_w = w - left_w
        out["rail"] = Rect(mid_y, 0, proj_h, left_w)
        out["work"] = Rect(mid_y + proj_h + _LEFT_GAP, 0,
                           max(0, mid_h - proj_h - _LEFT_GAP), left_w)
        out["inspector"] = Rect(mid_y, left_w, mid_h, insp_w)
    else:                                             # too narrow to go side-by-side: stack them
        out["rail"] = Rect(mid_y, 0, proj_h, w)
        rest = max(0, mid_h - proj_h - _LEFT_GAP)     # -gap = the blank separator row above WORK
        work_h = rest * 6 // 10                       # WORK gets the larger share of the leftover
        out["work"] = Rect(mid_y + proj_h + _LEFT_GAP, 0, work_h, w)
        out["inspector"] = Rect(mid_y + proj_h + _LEFT_GAP + work_h, 0, rest - work_h, w)
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


def split_bands(top, height, n):
    """Divide `height` rows starting at `top` into `n` contiguous (y, h) bands. Heights are as even
    as possible with the remainder given to the top bands; n <= 0 -> []. When height < n the trailing
    bands get h == 0. Sum of heights == max(0, height)."""
    if n <= 0:
        return []
    base, extra = divmod(max(0, height), n)
    out = []
    y = top
    for i in range(n):
        h = base + (1 if i < extra else 0)
        out.append((y, h))
        y += h
    return out


def render_pane_title(scr, rect, title, focused):
    """Draw a pane's title row. The FOCUSED pane gets a bright, high-contrast reverse bar with a ▶
    marker; unfocused panes recede to plain dim text — so the active pane is unmistakable and its
    title text stays readable. Returns the inner Rect below the title row."""
    if focused:
        attr = curses.A_REVERSE | curses.A_BOLD              # bright bar = the focused pane
        text = f"▶ {title}"
    else:
        attr = curses.A_DIM
        text = f"  {title}"
    _add(scr, rect.y, rect.x, pad_cols(text, rect.w), rect.x + rect.w, attr)
    return Rect(rect.y + 1, rect.x, max(0, rect.h - 1), rect.w)


# ── panel color palette: ONE color per ROLE, applied consistently across every pane ──
# (does NOT touch dashboard.STATUS_PAIR, which the classic UI shares)
_STRUCTURE = 3      # cyan   — section/band headers + the inspector's header lines
_LIVE = 1           # green  — running agents, the live project, succeeded/pass
_NEEDS_YOU = 4      # yellow — founder-gated work: gate rows, counts, chips, "prio high"
_BAD = 2            # red    — failed/error/blocked lines
_VITALS = 5         # magenta — the top vitals readout bar; a distinct background so it never reads
                    #           as a focused pane title (white reverse bar) or a band header (cyan)
# focus/selection  = curses.A_REVERSE | curses.A_BOLD (a bright bar; no color pair)
# inactive         = curses.A_DIM (history, parked, placeholders, labels)
_BAND_DIM = {"ARCHIVE", "DEFERRED"}                          # history/parked headers recede (inactive)
_DIM_STATUSES = {"done", "cancelled", "deferred"}            # backlog stays readable (it's the pull pool)

# ── status-dot vocabulary: one glyph per state, shared across vitals + rail (mission-control) ──
_DOT_RUN = "●"     # ● a running agent / live project   (green when active)
_DOT_GATE = "◆"    # ◆ founder-gated work — "needs you"  (yellow when >0)
_DOT_IDLE = "◇"    # ◇ idle / nominal                    (no accent)
_BRAND = "▸"       # ▸ identity separator


def _row_search_text(r):
    """Searchable text for a panel WORK row (used by the `/` filter)."""
    if r["kind"] == "running":
        return f"{r.get('task_id', '')} {r['project']} {r.get('agent', '')}"
    t = r.get("task")
    return f"{r.get('tag', '')} {r['id']} {r['project']} {t.title if t else ''}"


def _tag_attr(app, status):
    """A WORK row's base color BY ROLE (not per-status): founder gates are needs-you yellow,
    archive/parked recede to dim, ordinary in-flight work stays plain."""
    if status in _DIM_STATUSES:
        return curses.A_DIM                                  # archive/parked → inactive
    if status in d.GATE_ORDER:
        return app._cp(_NEEDS_YOU)                           # founder gate → needs-you yellow
    return 0                                                 # ordinary loop work → plain


def render_work(scr, rect, app, focused):
    """Panel-native WORK: band bars + color-tagged selectable rows."""
    inner = render_pane_title(scr, rect, "WORK", focused)
    rows = app.left_rows()
    sel_i, sel_row = app._selected(rows)
    app.sel_id = sel_row["id"] if sel_row else None
    base = max(0, sel_i - inner.h + 1) if rows else 0
    for idx, r in enumerate(rows[base:base + inner.h]):
        y = inner.y + idx
        if r["kind"] == "spacer":
            continue
        if r["kind"] == "band":
            name = r["label"].rsplit(" · ", 1)[0]       # "NEEDS YOU · 1" -> "NEEDS YOU"
            bar = pad_cols(f"▌ {r['label']} ", inner.w)
            if r.get("empty") or name in _BAND_DIM:
                battr = curses.A_DIM                          # empty / ARCHIVE / DEFERRED recede (nominal)
            else:                                            # every active band: one structure color
                battr = app._cp(_STRUCTURE) | curses.A_REVERSE | curses.A_BOLD
            _add(scr, y, inner.x, bar, inner.x + inner.w, battr)
            continue
        if r["kind"] == "info":
            _add(scr, y, inner.x, clip_cols(r["label"], inner.w), inner.x + inner.w,
                 curses.A_DIM)
            continue
        # task / running row: TAG  id  project  title
        selected = (base + idx) == sel_i and focused
        if r["kind"] == "running":
            tag, tid, proj = "RUN", (r.get("task_id") or "—"), r["project"]
            title = f"{r.get('agent','')}"
            base_attr = app._cp(_LIVE)                        # running = green (live)
        else:
            tag, tid, proj = r["tag"], r["id"], r["project"]
            title = r["task"].title
            base_attr = _tag_attr(app, r.get("status", ""))
        line = f"  {tag:<7} {tid:<8} {proj[:11]:<11} {title}"
        # selection is ONE uniform bright bar (same as the focused pane title), not the row's hue
        attr = (curses.A_REVERSE | curses.A_BOLD) if selected else base_attr
        _add(scr, y, inner.x, pad_cols(clip_cols(line, inner.w), inner.w),
             inner.x + inner.w, attr)


_RUN_LINE_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s")   # an inspector run row: a leading "HH:MM "

# the proposal/initiative template sub-heads (CLAUDE.md: WHAT · WHY NOW · EXPECTED IMPACT · SCOPE
# & COST · ALTERNATIVES). A CLOSED set: only these pop as headers, so arbitrary prose with a
# trailing ':' (e.g. "LEAD CALL:", "…NOTE:") is never miscolored.
_SUBHEADS = ("WHAT:", "WHY NOW:", "WHY:", "EXPECTED IMPACT:", "IMPACT:",
             "SCOPE & COST:", "SCOPE:", "ALTERNATIVES:", "HOW:", "NEXT:")
# longest-first + a word boundary so "EXPECTED IMPACT:" matches whole (not its inner "IMPACT:")
_SUBHEAD_RE = re.compile(r"\b(?:" + "|".join(re.escape(h) for h in
                         sorted(_SUBHEADS, key=len, reverse=True)) + r")")


def _split_subheads(line):
    """Break a run-on note line into pieces, each starting at a known sub-head (or the line head)."""
    cuts = sorted({0, len(line)} | {m.start() for m in _SUBHEAD_RE.finditer(line)})
    return [line[a:b] for a, b in zip(cuts, cuts[1:])]


def _inspector_attr(app, line):
    """Color INSPECTOR lines by STRUCTURE, not by substrings in free text. Only a run row (a leading
    HH:MM time) carries its status color; only the exact section headers are cyan; the generated
    assignee/prio meta line is dim (yellow when prio is high). Titles and note-body prose stay plain
    so text like 'password', 'NOTE:' or 'BLOCKED' is never miscolored."""
    s = line.strip()
    low = s.lower()
    if _RUN_LINE_RE.match(line):                            # a run row -> color by its status word
        if "succeeded" in low:
            return app._cp(_LIVE)
        if "fail" in low or "error" in low or "blocked" in low:
            return app._cp(_BAD)
        return 0
    if s == "notes:" or s.startswith("runs touching"):      # the only real section headers
        return app._cp(_STRUCTURE) | curses.A_BOLD
    if any(s.startswith(h) for h in _SUBHEADS):            # a known proposal sub-head pops as structure
        return app._cp(_STRUCTURE) | curses.A_BOLD
    if s.startswith("assignee ") and " · prio " in low:     # the generated meta line, matched precisely
        return app._cp(_NEEDS_YOU) if ("· prio high" in low or "· prio urgent" in low) else curses.A_DIM
    return 0                                                # title + note body: plain


def _panel_detail_lines(app, sel_row):
    """Inspector content as RAW logical lines. Unlike the inherited App.detail_lines (which
    pre-wraps notes to a fixed 56 cols and collapses the author's newlines), this preserves the
    author's own line breaks and does NOT wrap — render_inspector reflows once to the pane width."""
    snap = app.snap
    if not sel_row or not snap:
        return ["(nothing selected)"]
    by_name = {p.name: p for p in snap.projects}
    task = sel_row.get("task")
    p = by_name.get(sel_row.get("project"))
    if task is None or p is None:
        return app.detail_lines(sel_row)            # non-task rows: defer to the classic formatter
    out = [f"{task.id}  {task.status}",
           f'"{task.title}"',
           f"assignee {task.assignee or '-'} · prio {task.priority} · "
           f"pr {task.pr_url or '(none)'}",
           ""]
    if task.notes:
        out.append("notes:")
        for ln in task.notes.split("\n"):           # keep the author's structure; blanks stay blank
            if not ln.strip():
                out.append("")
                continue
            # break a run-on line before each known sub-head so WHAT/WHY NOW/IMPACT/… each start a
            # line and become a scannable outline (no-op when the author already broke them out)
            for piece in _split_subheads(ln):
                if piece.strip():
                    out.append("  " + piece.strip())
        out.append("")
    out.append(f"runs touching {task.id}:")
    for r in d.runs_touching(p.recent_runs, task.id):
        dur = f"{r.dur_min}m" if r.dur_min is not None else "··"
        out.append(f"  {d.to_local_hhmm(r.started_at):<5} {r.agent:<10} {r.status:<11} {dur:<4}")
    return out


def render_inspector(scr, rect, app, focused):
    """Detail of the current selection, wrapped to the pane width. Color-coded by line type.
    Running selections (task=None) show the agent header instead of crashing on task.id."""
    inner = render_pane_title(scr, rect, "INSPECTOR", focused)
    rows = app.left_rows()
    _, sel_row = app._selected(rows)
    # A running selection has task=None — detail_lines() does task.id and would CRASH.
    # Show the running agent's header instead (full live-log streaming is the log phase).
    if sel_row and sel_row.get("kind") == "running":
        lines = app.running_header(sel_row, app._now()) + \
            ["", "(press l to open this agent's log)"]
    else:
        lines = _panel_detail_lines(app, sel_row)
    wrapped = []
    for ln in lines:
        if disp_width(ln) <= inner.w:
            wrapped.append(ln)
        else:
            stripped = ln.lstrip(" ")
            lead = len(ln) - len(stripped)
            sub = " " * (lead + (2 if stripped.startswith("- ") else 0))   # align continuation
            wrapped.extend(textwrap.wrap(ln, inner.w, subsequent_indent=sub) or [""])
    start = max(0, min(app.detail_scroll, max(0, len(wrapped) - 1)))
    # first wrapped line is the "<id>  <status>" header — color it by the selection's status
    head_attr = 0
    if sel_row and sel_row.get("status"):
        head_attr = app._cp(_STRUCTURE) | curses.A_BOLD     # the id/status head line is a header
    for idx, ln in enumerate(wrapped[start:start + inner.h]):
        doc_i = start + idx
        attr = head_attr if doc_i == 0 else _inspector_attr(app, ln)
        _add(scr, inner.y + idx, inner.x, ln, inner.x + inner.w, attr)


def render_vitals(scr, rect, app):
    """The top cockpit readout: identity ▸ HERO(running · need you) · context(watch/proj/cooling) ·
    clock. The two operational numbers are the hero — ● N running goes green when agents are live,
    ◆ N NEED YOU goes yellow + UPPERCASE when work is gated (◇ when nominal) — so the strip reads calm
    when nothing needs you and alarms when there's a gate. The base bar is its OWN colour (_VITALS) so
    it never reads as a focused pane title (white reverse bar) or a band header (cyan). NO budget bar."""
    snap = app.snap
    ws = snap.workspace if snap else None
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
    pf = getattr(app, "project_filter", None)
    proj_seg = " · all projects" if pf is None else f" · {pf}"
    ident = f" DAIS {_BRAND} {ws}" if ws else " DAIS"     # honesty comes from the watch badge, not a literal "LIVE"
    run_dot = _DOT_RUN if threads else _DOT_IDLE
    run_tok = f"{run_dot} {len(threads)} running"
    gate_tok = f"{_DOT_GATE} {ng} NEED YOU" if ng > 0 else f"{_DOT_IDLE} {ng} need you"
    pre = ident + "   "
    sep = " · "                                            # between the two hero tokens
    ctx = f"   {badge} · {nproj} proj{proj_seg}{cool}  {clk}"
    line = pre + run_tok + sep + gate_tok + ctx
    bar = app._cp(_VITALS) | curses.A_REVERSE | curses.A_BOLD   # the readout's own background colour
    _add(scr, rect.y, rect.x, pad_cols(line, rect.w), rect.x + rect.w, bar)
    if threads:                                            # the run token glows green while agents are live
        _add(scr, rect.y, rect.x + disp_width(pre), run_tok, rect.x + rect.w,
             app._cp(_LIVE) | curses.A_REVERSE | curses.A_BOLD)
    if ng > 0:                                             # the gate token is the alarm: yellow hero
        _add(scr, rect.y, rect.x + disp_width(pre + run_tok + sep), gate_tok,
             rect.x + rect.w, app._cp(_NEEDS_YOU) | curses.A_REVERSE | curses.A_BOLD)


def _rail_items(app):
    names = [p.name for p in app.snap.projects] if app.snap else []
    return ["ALL"] + names


def render_rail(scr, rect, app, focused):
    """Project navigator: ALL + per-project rows in one uniform color; only the live (running)
    project is green; the active filter is bold with a » mark; the focused cursor is reverse.
    When more projects than fit, the body scrolls to keep the cursor visible (same bottom-anchored
    idiom as WORK) and the title badges the hidden count ("PROJECTS  +N") so the tail isn't silently
    truncated."""
    items = _rail_items(app)
    ri = getattr(app, "_rail_i", 0)
    body_h = max(0, rect.h - 1)                          # rows for items; the title takes the first
    base = max(0, ri - body_h + 1) if body_h else 0      # scroll so the selected row stays on-screen
    hidden = max(0, len(items) - body_h)                 # projects off-screen at any scroll position
    title = "PROJECTS" if not hidden else f"PROJECTS  +{hidden}"
    inner = render_pane_title(scr, rect, title, focused)
    pf = getattr(app, "project_filter", None)
    for vis_idx, name in enumerate(items[base:base + inner.h]):
        idx = base + vis_idx                            # index into items (cursor/active test on this)
        active = (name == "ALL" and pf is None) or name == pf
        mark = "\xbb" if active else " "                    # »
        p = (next((x for x in app.snap.projects if x.name == name), None)
             if (app.snap and name != "ALL") else None)
        running = bool(p and getattr(p, "running", False))
        hue = app._cp(_LIVE) if running else 0              # uniform: only a live project is colored
        attr = hue | (curses.A_BOLD if (active and name != "ALL") else 0) | \
            (curses.A_REVERSE if (focused and idx == ri) else 0)
        if p is not None:
            ng = sum(len(p.tasks_by_status.get(st, [])) for st in d.GATE_ORDER)
            run = _DOT_RUN if running else " "              # ● marks the live project (shared with vitals)
            name_str = f"{mark}{run}{name}"
            _add(scr, inner.y + vis_idx, inner.x, clip_cols(name_str, inner.w),
                 inner.x + inner.w, attr)
            if ng:                                          # needs-you count: bold yellow so it pops
                chip_attr = app._cp(_NEEDS_YOU) | curses.A_BOLD | \
                    (curses.A_REVERSE if (focused and idx == ri) else 0)
                _add(scr, inner.y + vis_idx, inner.x + disp_width(name_str), f" !{ng}",
                     inner.x + inner.w, chip_attr)
        else:
            _add(scr, inner.y + vis_idx, inner.x, clip_cols(f"{mark} {name}", inner.w),
                 inner.x + inner.w, attr)


def _feed_attr(app, status):
    """Activity-ticker color by run result, consistent with the panel palette."""
    if status == "succeeded":
        return app._cp(_LIVE)
    if status in ("failed", "capped"):
        return app._cp(_BAD)
    if status == "interrupted":
        return app._cp(_NEEDS_YOU)
    if status == "running":
        return app._cp(_LIVE) | curses.A_BOLD
    return 0


def render_feed(scr, rect, app):
    """One-line activity ticker: the most recent agent runs across the org (newest first), each
    colored by result. Straight from snap.recent_runs — honest, no fabrication."""
    runs = app.snap.recent_runs if app.snap else []
    x, end = rect.x, rect.x + rect.w
    label = " FEED  "
    _add(scr, rect.y, x, label, end, curses.A_DIM)
    x += disp_width(label)
    if not runs:
        _add(scr, rect.y, x, clip_cols("(no recent runs)", end - x), end, curses.A_DIM)
        return
    for i, r in enumerate(runs):
        if x >= end:
            break
        seg = f"{d.to_local_hhmm(r.started_at)} {r.agent} {r.status}"   # r.agent is already 'project/agent'
        sep = "  ·  " if i < len(runs) - 1 else ""
        text = clip_cols(seg + sep, end - x)
        if not text:
            break
        _add(scr, rect.y, x, text, end, _feed_attr(app, r.status))
        x += disp_width(text)


def render_logwall(scr, rect, app):
    """Full-body live log wall: one full-width band per running agent (green header + live tail).
    Reuses running_threads + tail_lines + _LOG_ERR_RE; tailed each draw so the text streams live."""
    threads = d.running_threads(app.snap, app._now()) if app.snap else []
    inner = render_pane_title(scr, rect, f"LOG WALL · {len(threads)} agents", True)
    if not threads:
        _add(scr, inner.y, inner.x, clip_cols("  (no agents running)", inner.w),
             inner.x + inner.w, curses.A_DIM)
        return
    n = len(threads)
    if n > inner.h:                          # not every agent fits — reserve the last row for the note
        shown, note_n = max(0, inner.h - 1), n - max(0, inner.h - 1)
    else:
        shown, note_n = n, 0
    band_h = inner.h - (1 if note_n else 0)  # leave the last row free when a note will be drawn
    for t, (by, bh) in zip(threads, split_bands(inner.y, band_h, shown)):
        if bh <= 0:
            continue
        tid = t.get("task") or "—"
        head = f"▶ {t['project']}/{t['agent']} · running {d.fmt_elapsed(t.get('secs') or 0)} · {tid}"
        _add(scr, by, inner.x, pad_cols(head, inner.w), inner.x + inner.w,
             app._cp(_LIVE) | curses.A_REVERSE | curses.A_BOLD)
        k = bh - 1
        if k <= 0:
            continue
        lines = d.tail_lines(t.get("log_path"), k)
        if not lines:
            _add(scr, by + 1, inner.x, clip_cols("  (waiting for output...)", inner.w),
                 inner.x + inner.w, curses.A_DIM)
            continue
        for i, ln in enumerate(lines):
            attr = app._cp(_BAD) if d._LOG_ERR_RE.search(ln) else 0
            _add(scr, by + 1 + i, inner.x, clip_cols("  " + ln, inner.w),
                 inner.x + inner.w, attr)
    if note_n:
        _add(scr, inner.y + inner.h - 1, inner.x,
             clip_cols(f"  +{note_n} more agent(s) - resize to see", inner.w),
             inner.x + inner.w, curses.A_DIM)


def render_bar(scr, rect, app, focus):
    """Contextual action bar (reused) + the panel's global keys."""
    if getattr(app, "show_logwall", False):
        _add(scr, rect.y, rect.x, pad_cols(" L/esc back · q quit", rect.w),
             rect.x + rect.w, curses.A_REVERSE)
        return
    rows = app.left_rows()
    _, sel_row = app._selected(rows)
    acts = app.action_bar(sel_row) if sel_row else ""
    keys = "tab pane · / filter · g expand · L logs · ? help · q quit"
    if getattr(app, "filtering", False):
        hint = f" /{app.filter}_  ·  {keys}"
    else:
        hint = f" {acts}  ·  {keys}"
    _add(scr, rect.y, rect.x, pad_cols(hint, rect.w), rect.x + rect.w, curses.A_REVERSE)


_HELP_LINES = [
    "  KEYS",
    "  tab / shift-tab   move focus between panes",
    "  j / k             move selection (or scroll inspector)",
    "  a / x             advance / reverse the selected task",
    "  + / -             raise / lower priority",
    "  o                 open the PR in a browser",
    "  n                 new task",
    "  enter             action menu for the selection",
    "  / filter          filter; type, enter to keep, esc to clear",
    "  g expand          show the full backlog, deferred + archive (else compact)",
    "  rail + j/k        pick a project (ALL clears the filter)",
    "  l                 open the log pager for the selection",
    "  L logs            live log wall - all running agents (esc back)",
    "  ? help            this overlay (any key closes)",
    "  q                 quit",
]


def render_overlay(scr, h, w, ov):
    """Centered reverse-video modal showing a captured command's output (a bold title row, then the
    output tailed to fit so the footer stays visible). Lets actions like `ship` run IN the panel
    instead of dropping to the console. Any key dismisses it."""
    bw = min(max(0, w - 4), 100)
    lines = ov.get("lines", []) if ov else []
    max_body = max(1, min(h - 2, len(lines) + 1) - 1)    # leave a row for the title
    body = lines[-max_body:] if len(lines) > max_body else lines
    box = [ov.get("title", "") if ov else ""] + body
    bh = min(max(0, h - 2), len(box))
    y0 = max(0, (h - bh) // 2)
    x0 = max(0, (w - bw) // 2)
    for i in range(bh):
        attr = curses.A_REVERSE | (curses.A_BOLD if i == 0 else 0)
        _add(scr, y0 + i, x0, pad_cols(clip_cols(box[i], bw), bw), x0 + bw, attr)


def render_help(scr, h, w):
    """Centered keymap overlay."""
    bw = min(w - 4, 56)
    bh = min(h - 2, len(_HELP_LINES) + 2)
    y0 = max(0, (h - bh) // 2)
    x0 = max(0, (w - bw) // 2)
    for i in range(bh):
        line = _HELP_LINES[i] if i < len(_HELP_LINES) else ""
        attr = curses.A_REVERSE | (curses.A_BOLD if i == 0 else 0)
        _add(scr, y0 + i, x0, pad_cols(line, bw), x0 + bw, attr)


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
        self._panel_expanded = False
        self._rail_i = 0
        self.project_filter = None
        self.show_help = False
        self.show_logwall = False
        self.show_overlay = False        # an action's captured output, shown in-panel (e.g. ship)
        self._overlay = None

    def left_rows(self):
        rows = panel_work_rows(self.snap, project=self.project_filter,
                               expanded=self._panel_expanded)
        if self.filter:                          # honest filter: narrow to matching task/running rows
            rows = [r for r in rows if r["kind"] in ("task", "running")]
            rows = d.filter_rows(rows, self.filter, key=_row_search_text)
        return rows

    def _capture(self, cmd):
        """Run a non-interactive `dais …` command capturing its output. Returns (rc, text)."""
        r = d.subprocess.run([self._dais()] + [str(c) for c in cmd],
                             capture_output=True, text=True, stdin=d.subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def _ship_pr(self, row, cmd):
        """Override the inherited console-drop: run ship IN the panel by capturing its output into a
        dismissible overlay. ship is non-interactive, so this can't hang; the merge itself (argv, QA
        gate, the prior _confirm) is unchanged — only the output presentation differs."""
        tid = (self._task_of(row) or {}).get("id") or "?"
        label = "dais " + " ".join(str(c) for c in cmd)
        self._overlay = {"title": f"shipping {tid} …",
                         "lines": [f"running: {label}", "", "please wait — merging…"]}
        self.show_overlay = True
        try:                                     # paint the 'running' frame; the capture below blocks
            h, w = self.scr.getmaxyx()
            render_overlay(self.scr, h, w, self._overlay)
            self.scr.refresh()
        except curses.error:
            pass
        rc, out = self._capture(cmd)
        verdict = "done" if rc == 0 else f"FAILED (exit {rc})"
        self._overlay = {"title": f"ship {tid} — {verdict}",
                         "lines": (out.splitlines() or ["(no output)"])
                         + ["", f"[exit {rc}]  press any key to dismiss"]}
        self.show_overlay = True
        return rc

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if self.show_logwall:
            render_vitals(scr, Rect(0, 0, 1, w), self)
            render_logwall(scr, Rect(1, 0, max(1, h - 2), w), self)
            render_bar(scr, Rect(h - 1, 0, 1, w), self, self.pane_focus)
            if self.show_help:
                render_help(scr, h, w)
            scr.refresh()
            return
        rects = layout(w, h, n_rail_items=len(_rail_items(self)), show_feed=True)
        order = focus_order(rects)
        if self.pane_focus not in order:
            self.pane_focus = order[0] if order else "work"
        for pid, rect in rects.items():
            if pid == "bar":
                render_bar(scr, rect, self, self.pane_focus)
            else:
                _RENDER[pid](scr, rect, self, pid == self.pane_focus)
        if self.show_overlay and self._overlay:
            render_overlay(scr, h, w, self._overlay)
        if self.show_help:
            render_help(scr, h, w)
        scr.refresh()

    def handle(self, ch, rows, sel_i, sel_row):
        # filtering takes priority — all keystrokes route to the inherited filter handler
        if self.filtering:
            return super().handle(ch, rows, sel_i, sel_row)
        if self.show_overlay:                   # action-output overlay (e.g. ship): any key returns
            self.show_overlay = False
            return True
        if self.show_help:                      # any key dismisses the overlay
            self.show_help = False
            return True
        if ch == ord("?"):
            self.show_help = True
            return True
        if self.show_logwall:                   # the wall is a passive full-body view
            if ch == ord("q"):
                return False
            if ch in (ord("L"), 27):            # L or esc returns to the control panel
                self.show_logwall = False
            return True
        if ch == ord("L"):                      # open the live log wall
            self.show_logwall = True
            return True
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
        if ch == ord("g"):                      # expand/collapse loop + archive
            self._panel_expanded = not self._panel_expanded
            return True
        if self.pane_focus == "rail" and ch in (ord("j"), ord("k"),
                                                curses.KEY_DOWN, curses.KEY_UP):
            items = _rail_items(self)
            step = 1 if ch in (ord("j"), curses.KEY_DOWN) else -1
            self._rail_i = max(0, min(self._rail_i + step, len(items) - 1))
            self.project_filter = None if items[self._rail_i] == "ALL" else items[self._rail_i]
            self.sel_id = None                  # reset work selection to the filtered top
            return True
        if ch == ord("b"):                      # 'b' (classic show/hide-parked) is dead here —
            return True                         # backlog + deferred are first-class sections now
        # everything else (j/k select, a/x/+/-/n/o/enter, /, ...) reuses App.handle,
        # which already drives selection + the action engine on `rows`/`sel_row`.
        self.focus = "left"                     # App.handle's left-pane selection path
        return super().handle(ch, rows, sel_i, sel_row)


# WORK row model — bands of selectable rows; status → terminal-safe tag (no emoji)
GATE_TAG = {"ready_to_merge": "MERGE", "needs_review": "REVIEW",
            "proposed": "PROPOSE", "blocked": "BLOCKED"}
_LOOP_STATUSES = d.LOOP_SUMMARY_ORDER          # ["ready","needs_qa","changes_requested"]
_ARCHIVE_STATUSES = ["done", "cancelled"]
_BACKLOG_STATUSES = ["backlog"]
_DEFERRED_STATUSES = ["deferred"]
_ARCHIVE_CAP = 12
_BACKLOG_CAP = 8


def _band(name, count):
    return {"kind": "band", "id": f"__band::{name}", "sel": False,
            "label": f"{name} · {count}", "empty": count == 0}


def _task_row(proj, task, tag):
    return {"kind": "task", "id": task.id, "project": proj, "task": task,
            "status": task.status, "tag": tag, "sel": True}


def panel_work_rows(snap, *, project=None, expanded=False):
    """The panel's WORK list: ordered bands of selectable rows (RUNNING · NEEDS YOU · IN FLIGHT ·
    BACKLOG · DEFERRED · ARCHIVE). `project` limits to one project. `expanded` (the g key) shows
    the full BACKLOG, reveals DEFERRED rows, and uncaps the ARCHIVE. An empty RUNNING/NEEDS YOU/
    IN FLIGHT band collapses to just its dim header (no '(none …)' filler)."""
    rows = []
    if not snap:
        return rows
    projects = [p for p in snap.projects if project is None or p.name == project]

    def tasks_in(statuses):
        out = []
        for p in projects:
            for st in statuses:
                for t in p.tasks_by_status.get(st, []):
                    if (p.name, t.id) in running_ids:    # a running task shows only in RUNNING
                        continue
                    out.append((p.name, t))
        return out

    def add_band(name, count):
        if rows:                                    # no leading spacer above the very first band
            rows.append({"kind": "spacer", "id": f"__sp::{name}", "sel": False, "label": ""})
        rows.append(_band(name, count))

    # RUNNING — an empty band collapses to just its dim header (no "(none …)" filler), so the
    # screen reads calm when nominal and only the live/gated bands draw the eye.
    now = "9999-12-31 00:00:00"            # elapsed not needed for the model; render computes it
    threads = [t for t in d.running_threads(snap, now)
               if project is None or t["project"] == project]
    running_ids = {(t["project"], t["task"]) for t in threads if t["task"]}
    add_band("RUNNING", len(threads))
    for t in threads:
        rows.append({"kind": "running", "id": f"run::{t['project']}",
                     "project": t["project"], "task_id": t["task"], "task": None,
                     "status": "doing", "sel": True, "agent": t["agent"],
                     "since": t["since"], "log_path": t["log_path"]})

    # NEEDS YOU (the founder gates)
    gates = tasks_in(d.GATE_ORDER)
    add_band("NEEDS YOU", len(gates))
    for proj, t in gates:
        rows.append(_task_row(proj, t, GATE_TAG.get(t.status, t.status.upper())))

    # IN FLIGHT — the in-flight work, shown as rows by default (no g needed)
    loop = tasks_in(_LOOP_STATUSES)
    add_band("IN FLIGHT", len(loop))
    for proj, t in loop:
        rows.append(_task_row(proj, t, t.status))

    # BACKLOG — the queue-able pool, always visible so you can pull from it (a promotes -> ready)
    backlog = tasks_in(_BACKLOG_STATUSES)
    add_band("BACKLOG", len(backlog))
    if backlog:
        shown = backlog if expanded else backlog[:_BACKLOG_CAP]
        for proj, t in shown:
            rows.append(_task_row(proj, t, "BACK"))
        if len(backlog) > len(shown):
            rows.append({"kind": "info", "id": "__backlog_more", "sel": False,
                         "label": f"  +{len(backlog) - len(shown)} more   (press g to show all)"})
    else:
        rows.append({"kind": "info", "id": "__backlog_none", "sel": False,
                     "label": "  (backlog empty)"})

    # DEFERRED — founder-parked; its own section, collapsed to a count until expanded (g)
    deferred = tasks_in(_DEFERRED_STATUSES)
    add_band("DEFERRED", len(deferred))
    if expanded:
        for proj, t in deferred:
            rows.append(_task_row(proj, t, "DEFER"))
    elif deferred:
        rows.append({"kind": "info", "id": "__deferred_sum", "sel": False,
                     "label": f"  {len(deferred)} parked   (press g to show)"})

    # ARCHIVE — history at the very bottom; shown when a project is picked or expanded; g UNCAPS it
    if expanded or project is not None:
        arch = tasks_in(_ARCHIVE_STATUSES)
        add_band("ARCHIVE", len(arch))
        shown = arch if expanded else arch[:_ARCHIVE_CAP]    # g shows the full history
        for proj, t in shown:
            tag = "DONE" if t.status == "done" else "CANC"
            rows.append(_task_row(proj, t, tag))
        if len(arch) > len(shown):
            rows.append({"kind": "info", "id": "__arch_more", "sel": False,
                         "label": f"  +{len(arch) - len(shown)} older   (press g to show all)"})
    return rows
