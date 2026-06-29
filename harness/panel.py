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
        insp_w = max(36, (w - x) // 2)
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


def render_pane_title(scr, rect, title, focused, app):
    """Draw a pane's title row. The FOCUSED pane gets a magenta accent bar with a ▶ marker; unfocused
    panes recede to plain dim text — so the active pane is unmistakable. Returns the inner Rect below."""
    if focused:
        attr = app._cp(5) | curses.A_REVERSE | curses.A_BOLD     # magenta bar = the focused pane
        text = f"▶ {title}"
    else:
        attr = curses.A_DIM
        text = f"  {title}"
    _add(scr, rect.y, rect.x, pad_cols(text, rect.w), rect.x + rect.w, attr)
    return Rect(rect.y + 1, rect.x, max(0, rect.h - 1), rect.w)


# panel-local color system (does NOT touch dashboard.STATUS_PAIR, which the classic UI shares)
_BAND_PAIR = {"RUNNING": 1, "NEEDS YOU": 4, "THE LOOP": 3}   # green / yellow / cyan
_BAND_DIM = {"ARCHIVE", "PARKED"}                            # history/parked recede
_DIM_STATUSES = {"done", "cancelled", "backlog", "deferred"}
_RAIL_HUES = [3, 5, 4, 1, 2, 6]                              # cyan magenta yellow green red white


def _row_search_text(r):
    """Searchable text for a panel WORK row (used by the `/` filter)."""
    if r["kind"] == "running":
        return f"{r.get('task_id', '')} {r['project']} {r.get('agent', '')}"
    t = r.get("task")
    return f"{r.get('tag', '')} {r['id']} {r['project']} {t.title if t else ''}"


def _tag_attr(app, status):
    if status in _DIM_STATUSES:
        return curses.A_DIM                                  # archive/parked recede
    if status == "needs_review":
        return app._cp(3)                                    # cyan gate (override white)
    return app._cp(d.STATUS_PAIR.get(status, 6))


def render_work(scr, rect, app, focused):
    """Panel-native WORK: band bars + color-tagged selectable rows."""
    inner = render_pane_title(scr, rect, "WORK", focused, app)
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
            if name in _BAND_DIM:
                battr = curses.A_DIM
            else:
                battr = app._cp(_BAND_PAIR.get(name, 6)) | curses.A_REVERSE | curses.A_BOLD
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
            base_attr = app._cp(1)                           # live = green
        else:
            tag, tid, proj = r["tag"], r["id"], r["project"]
            title = r["task"].title
            base_attr = _tag_attr(app, r.get("status", ""))
        line = f"  {tag:<7} {tid:<8} {proj[:11]:<11} {title}"
        attr = (base_attr | curses.A_REVERSE) if selected else base_attr
        _add(scr, y, inner.x, pad_cols(clip_cols(line, inner.w), inner.w),
             inner.x + inner.w, attr)


def _inspector_attr(app, idx_in_doc, line):
    """Color by line type: run 'succeeded' green; 'fail'/'error'/'blocked' red; 'pass'/'+ ' green;
    section headers (':' / 'runs touching') cyan-bold; 'prio high/urgent' yellow; labels dim."""
    s = line.strip()
    low = s.lower()
    if "succeeded" in low:
        return app._cp(1)
    if "fail" in low or "error" in low or "blocked" in low:
        return app._cp(2)
    if "pass" in low or s.startswith("+ "):
        return app._cp(1)
    if s.endswith(":") or s.startswith("runs touching"):
        return app._cp(3) | curses.A_BOLD
    if "prio high" in low or "prio urgent" in low:
        return app._cp(4)
    if s.startswith("assignee") or s.startswith("prio"):
        return curses.A_DIM
    return 0


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
            out.append("  " + ln if ln.strip() else "")
        out.append("")
    out.append(f"runs touching {task.id}:")
    for r in d.runs_touching(p.recent_runs, task.id):
        dur = f"{r.dur_min}m" if r.dur_min is not None else "··"
        out.append(f"  {d.to_local_hhmm(r.started_at):<5} {r.agent:<10} {r.status:<11} {dur:<4}")
    return out


def render_inspector(scr, rect, app, focused):
    """Detail of the current selection, wrapped to the pane width. Color-coded by line type.
    Running selections (task=None) show the agent header instead of crashing on task.id."""
    inner = render_pane_title(scr, rect, "INSPECTOR", focused, app)
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
        head_attr = app._cp(d.STATUS_PAIR.get(sel_row["status"], 6)) | curses.A_BOLD
    for idx, ln in enumerate(wrapped[start:start + inner.h]):
        doc_i = start + idx
        attr = head_attr if doc_i == 0 else _inspector_attr(app, doc_i, ln)
        _add(scr, inner.y + idx, inner.x, ln, inner.x + inner.w, attr)


def render_vitals(scr, rect, app):
    """Honest org vitals: identity · watch · running/idle · cooling · gates · clock. NO budget bar.
    The 'N need you' token is highlighted yellow when N>0."""
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
    pf = getattr(app, "project_filter", None)
    proj_seg = " · all projects" if pf is None else f" · {pf}"
    left = f" {ident}  {badge} · >{len(threads)} running · {nproj} proj · "
    mid = f"{ng} need you"
    right = f"{proj_seg}{cool}  {clk}"
    _add(scr, rect.y, rect.x, pad_cols(left + mid + right, rect.w),
         rect.x + rect.w, curses.A_REVERSE | curses.A_BOLD)
    if ng > 0:
        x = rect.x + disp_width(left)
        _add(scr, rect.y, x, mid, rect.x + rect.w,
             app._cp(4) | curses.A_REVERSE | curses.A_BOLD)


def _rail_items(app):
    names = [p.name for p in app.snap.projects] if app.snap else []
    return ["ALL"] + names


def render_rail(scr, rect, app, focused):
    """Project navigator: ALL + per-project rows, each a distinct hue; the live (running) project is
    green; the active filter is bold with a » mark; the focused cursor is reverse."""
    inner = render_pane_title(scr, rect, "PROJECTS", focused, app)
    items = _rail_items(app)
    ri = getattr(app, "_rail_i", 0)
    pf = getattr(app, "project_filter", None)
    for idx, name in enumerate(items[:inner.h]):
        active = (name == "ALL" and pf is None) or name == pf
        mark = "\xbb" if active else " "                    # »
        p = (next((x for x in app.snap.projects if x.name == name), None)
             if (app.snap and name != "ALL") else None)
        running = bool(p and getattr(p, "running", False))
        if name == "ALL":
            hue = 0
        elif running:
            hue = app._cp(1)                                # live project = green
        else:
            hue = app._cp(_RAIL_HUES[(idx - 1) % len(_RAIL_HUES)])
        attr = hue | (curses.A_BOLD if (active and name != "ALL") else 0) | \
            (curses.A_REVERSE if (focused and idx == ri) else 0)
        if p is not None:
            ng = sum(len(p.tasks_by_status.get(st, [])) for st in d.GATE_ORDER)
            run = ">" if running else " "
            label = f"{mark}{run}{name}" + (f" !{ng}" if ng else "")
        else:
            label = f"{mark} {name}"
        _add(scr, inner.y + idx, inner.x, clip_cols(label, inner.w), inner.x + inner.w, attr)


def render_feed(scr, rect, app):
    """Phase-A placeholder line; the real activity feed lands in the feed/vitals phase."""
    _add(scr, rect.y, rect.x, pad_cols(" FEED  (activity ticker — coming next phase)",
         rect.w), rect.x + rect.w, curses.A_DIM)


def render_bar(scr, rect, app, focus):
    """Contextual action bar (reused) + the panel's global keys."""
    rows = app.left_rows()
    _, sel_row = app._selected(rows)
    acts = app.action_bar(sel_row) if sel_row else ""
    keys = "tab pane · / filter · b parked · g expand · ? help · q quit"
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
    "  b parked          show/hide backlog + deferred",
    "  g expand          expand the loop + archive (history)",
    "  rail + j/k        pick a project (ALL clears the filter)",
    "  l                 open the log pager for the selection",
    "  ? help            this overlay (any key closes)",
    "  q                 quit",
]


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

    def left_rows(self):
        rows = panel_work_rows(self.snap, project=self.project_filter,
                               expanded=self._panel_expanded, show_parked=self.show_parked)
        if self.filter:                          # honest filter: narrow to matching task/running rows
            rows = [r for r in rows if r["kind"] in ("task", "running")]
            rows = d.filter_rows(rows, self.filter, key=_row_search_text)
        return rows

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
        if self.show_help:
            render_help(scr, h, w)
        scr.refresh()

    def handle(self, ch, rows, sel_i, sel_row):
        # filtering takes priority — all keystrokes route to the inherited filter handler
        if self.filtering:
            return super().handle(ch, rows, sel_i, sel_row)
        if self.show_help:                      # any key dismisses the overlay
            self.show_help = False
            return True
        if ch == ord("?"):
            self.show_help = True
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
        # everything else (j/k select, a/x/+/-/n/o/enter, /, b, ...) reuses App.handle,
        # which already drives selection + the action engine on `rows`/`sel_row`.
        self.focus = "left"                     # App.handle's left-pane selection path
        return super().handle(ch, rows, sel_i, sel_row)


# WORK row model — bands of selectable rows; status → terminal-safe tag (no emoji)
GATE_TAG = {"ready_to_merge": "MERGE", "needs_review": "REVIEW",
            "proposed": "PROPOSE", "blocked": "BLOCKED"}
_LOOP_STATUSES = d.LOOP_SUMMARY_ORDER          # ["ready","needs_qa","changes_requested"]
_ARCHIVE_STATUSES = ["done", "cancelled"]
_ARCHIVE_CAP = 12


def _band(name, count):
    return {"kind": "band", "id": f"__band::{name}", "sel": False,
            "label": f"{name} · {count}"}


def _task_row(proj, task, tag):
    return {"kind": "task", "id": task.id, "project": proj, "task": task,
            "status": task.status, "tag": tag, "sel": True}


def panel_work_rows(snap, *, project=None, expanded=False, show_parked=False):
    """The panel's WORK list: ordered bands of selectable rows. `project` limits to one
    project; `expanded` reveals loop rows + an ARCHIVE band; `show_parked` adds PARKED."""
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

    # RUNNING
    now = "9999-12-31 00:00:00"            # elapsed not needed for the model; render computes it
    threads = [t for t in d.running_threads(snap, now)
               if project is None or t["project"] == project]
    running_ids = {(t["project"], t["task"]) for t in threads if t["task"]}
    add_band("RUNNING", len(threads))
    if threads:
        for t in threads:
            rows.append({"kind": "running", "id": f"run::{t['project']}",
                         "project": t["project"], "task_id": t["task"], "task": None,
                         "status": "doing", "sel": True, "agent": t["agent"],
                         "since": t["since"], "log_path": t["log_path"]})
    else:
        rows.append({"kind": "info", "id": "__run_none", "sel": False, "label": "  (none running)"})

    # NEEDS YOU (the founder gates)
    gates = tasks_in(d.GATE_ORDER)
    add_band("NEEDS YOU", len(gates))
    if gates:
        for proj, t in gates:
            rows.append(_task_row(proj, t, GATE_TAG.get(t.status, t.status.upper())))
    else:
        rows.append({"kind": "info", "id": "__gate_none", "sel": False,
                     "label": "  (nothing needs you)"})

    # THE LOOP — collapsed summary, or rows when expanded
    loop = tasks_in(_LOOP_STATUSES)
    add_band("THE LOOP", len(loop))
    if expanded and loop:
        for proj, t in loop:
            rows.append(_task_row(proj, t, t.status))
    else:
        seg = _loop_seg(loop)                    # count from the deduped loop list → matches the band header
        rows.append({"kind": "info", "id": "__loop_sum", "sel": False,
                     "label": "  " + (seg or "0 in flight") + "   (press g to expand)"})

    # ARCHIVE — only when expanded or a project is picked
    if expanded or project is not None:
        arch = tasks_in(_ARCHIVE_STATUSES)
        add_band("ARCHIVE", len(arch))
        for proj, t in arch[:_ARCHIVE_CAP]:
            tag = "DONE" if t.status == "done" else "CANC"
            rows.append(_task_row(proj, t, tag))
        if len(arch) > _ARCHIVE_CAP:
            rows.append({"kind": "info", "id": "__arch_more", "sel": False,
                         "label": f"  +{len(arch) - _ARCHIVE_CAP} older"})

    # PARKED — backlog + deferred, only on `b`
    if show_parked:
        parked = tasks_in(d.PARKED_ORDER)
        add_band("PARKED", len(parked))
        for proj, t in parked:
            rows.append(_task_row(proj, t, t.status.upper()[:6]))
    return rows


def _loop_seg(loop_tasks):
    """Collapsed THE-LOOP summary text built from the already running-deduped loop task list, so the
    count here always matches the THE LOOP band header. `loop_tasks` is a list of (project, task)
    tuples. No emoji, no "g for full board" suffix. Returns None when empty."""
    counts = {}
    for _, t in loop_tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    segs = [f"{counts[st]} {st}" for st in _LOOP_STATUSES if counts.get(st)]
    return "the loop: " + " · ".join(segs) if segs else None
