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
    """Height of the PROJECTS block (a title row + a column-header row + one row per rail item),
    capped so WORK keeps room and never taller than the middle band."""
    want = 2 + max(1, n_rail_items)
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
_VITALS = 8         # the top vitals readout bar (dashboard pair 8 = bold white on blue): a distinct,
                    # high-contrast bar so it never reads as a focused pane title (white reverse bar)
                    # or a band header (cyan)
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
            blocked = getattr(r["task"], "blocked", False)
        else:
            tag, tid, proj = r["tag"], r["id"], r["project"]
            title = r["task"].title
            base_attr = _tag_attr(app, r.get("status", ""))
            blocked = getattr(r["task"], "blocked", False)
        if r["kind"] != "running" and blocked:                # ⛓ a task waiting on an unfinished predecessor
            title = "⛓ " + title
        line = f"  {tag:<7} {tid:<8} {proj[:11]:<11} {title}"
        # selection is ONE uniform bright bar (same as the focused pane title), not the row's hue;
        # a blocked task dims (it won't be picked up until its predecessor is done).
        attr = (curses.A_REVERSE | curses.A_BOLD) if selected else (curses.A_DIM if blocked else base_attr)
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
           f"pr {task.pr_url or '(none)'}"]
    if getattr(task, "blocked", False):                 # waiting on an unfinished predecessor
        out.append(f"⛓ blocked on {task.blocked_on} — won't run until it's done")
    out.append("")
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


def _find_task(snap, tid):
    """The Task with this id anywhere in the snapshot (running rows carry only a task_id), or None."""
    if not (snap and tid):
        return None
    for p in snap.projects:
        for ts in p.tasks_by_status.values():
            for t in ts:
                if t.id == tid:
                    return t
    return None


def _note_lines(task, width):
    """The task's spec/notes as display lines (wrapped to width), sub-heads broken out — so while an
    agent runs you can see WHAT it's working from, not just the log. Empty when there are no notes."""
    if not task or not task.notes:
        return []
    out = ["notes:"]
    for ln in task.notes.split("\n"):
        if not ln.strip():
            out.append("")
            continue
        for piece in _split_subheads(ln):
            if piece.strip():
                out.extend(d.wrap_cols("  " + piece.strip(), width, subsequent_indent="    "))
    return out


def render_inspector_live_log(scr, inner, app, sel_row):
    """The running selection's inspector: a short header, then the task's NOTES/spec (so you can see
    what it's working from), a dim separator, then the LIVE LOG tail filling the rest — re-read every
    draw so it streams, errors red (same vocabulary as the log wall). Notes are capped to ~half the
    space so the log always keeps room; the whole thing replaces the old 'press l' hint."""
    bottom = inner.y + inner.h
    y = inner.y
    for i, ln in enumerate(app.running_header(sel_row, app._now())):
        if y >= bottom:
            return
        attr = (app._cp(_STRUCTURE) | curses.A_BOLD) if i == 0 else _inspector_attr(app, ln)
        _add(scr, y, inner.x, clip_cols(ln, inner.w), inner.x + inner.w, attr)
        y += 1
    notes = _note_lines(_find_task(app.snap, sel_row.get("task_id")), inner.w)
    if notes and bottom - y > 8:                    # only if there's room for notes AND a usable log
        cap = max(3, (bottom - y - 4) // 2)         # notes take ≤ ~half; the log gets the rest
        for ln in notes[:cap]:
            _add(scr, y, inner.x, clip_cols(ln, inner.w), inner.x + inner.w, _inspector_attr(app, ln))
            y += 1
        if len(notes) > cap:
            _add(scr, y, inner.x, clip_cols("  …", inner.w), inner.x + inner.w, curses.A_DIM)
            y += 1
    log_h = bottom - y - 1                          # reserve the row below for the live-log header
    if log_h <= 0:
        return
    # WRAP each log line to the pane width (no more cut-off), coloring continuation rows like their
    # source line; then the inspector's detail_scroll scrolls UP from the tail through this history.
    raw = d.tail_lines(sel_row.get("log_path"), 400) if sel_row.get("log_path") else []
    wrapped = []
    for ln in raw:
        a = app._log_attr(ln)
        for piece in (d.wrap_cols(ln, inner.w) or [ln]):
            wrapped.append((piece, a))
    offset = max(0, min(getattr(app, "detail_scroll", 0), max(0, len(wrapped) - log_h)))
    app.detail_scroll = offset                      # clamp (so j past the tail / k past the top stick)
    head = "─ live log ─" if offset == 0 else f"─ live log · ↑{offset} (j → follow) ─"
    _add(scr, y, inner.x, clip_cols(head, inner.w), inner.x + inner.w, curses.A_DIM)
    y += 1
    if not wrapped:
        _add(scr, y, inner.x, clip_cols("  (waiting for output…)", inner.w),
             inner.x + inner.w, curses.A_DIM)
        return
    end = len(wrapped) - offset
    for i, (txt, a) in enumerate(wrapped[max(0, end - log_h):end]):
        _add(scr, y + i, inner.x, clip_cols(txt, inner.w), inner.x + inner.w, a)


def render_inspector(scr, rect, app, focused):
    """Detail of the current selection, wrapped to the pane width. Color-coded by line type.
    Running selections stream their live log (render_inspector_live_log) instead of task detail."""
    inner = render_pane_title(scr, rect, "INSPECTOR", focused)
    rows = app.left_rows()
    _, sel_row = app._selected(rows)
    # A running selection streams its LIVE LOG right here (no need to pop the `l` pager or `L` wall) —
    # a short agent header, then the log tail, re-read each draw so it streams as the agent works.
    if sel_row and sel_row.get("kind") == "running":
        render_inspector_live_log(scr, inner, app, sel_row)
        return
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
    threads = d.running_threads(snap, now, app.root) if snap else []
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
    pend = [p for p in (snap.projects if snap else []) if (p.deploy_pending or 0) > 0]
    n_deploy = len(pend)
    mig_pending = any(getattr(p, "deploy_migration", False) for p in pend)
    run_dot = _DOT_RUN if threads else _DOT_IDLE
    run_tok = f"{run_dot} {len(threads)} running"
    gate_tok = f"{_DOT_GATE} {ng} NEED YOU" if ng > 0 else f"{_DOT_IDLE} {ng} need you"
    deploy_tok = (f" · ⬆ {n_deploy} DEPLOY{' ⚠MIGRATION' if mig_pending else ''}"
                  if n_deploy else "")        # merged-but-not-deployed: a founder gate; ⚠ if it has a migration
    pre = ident + "   "
    sep = " · "                                            # between the two hero tokens
    ctx = f"   {badge} · {nproj} proj{proj_seg}{cool}  {clk}"
    line = pre + run_tok + sep + gate_tok + deploy_tok + ctx
    bar = app._cp(_VITALS) | curses.A_BOLD                 # the readout's own bar (bold white on blue)
    _add(scr, rect.y, rect.x, pad_cols(line, rect.w), rect.x + rect.w, bar)
    if threads:                                            # the run token glows green while agents are live
        _add(scr, rect.y, rect.x + disp_width(pre), run_tok, rect.x + rect.w,
             app._cp(_LIVE) | curses.A_REVERSE | curses.A_BOLD)
    if ng > 0:                                             # the gate token is the alarm: yellow hero
        _add(scr, rect.y, rect.x + disp_width(pre + run_tok + sep), gate_tok,
             rect.x + rect.w, app._cp(_NEEDS_YOU) | curses.A_REVERSE | curses.A_BOLD)
    if n_deploy:                                           # un-deployed merges also pull the eye yellow
        _add(scr, rect.y, rect.x + disp_width(pre + run_tok + sep + gate_tok), deploy_tok,
             rect.x + rect.w, app._cp(_NEEDS_YOU) | curses.A_REVERSE | curses.A_BOLD)


def _rail_items(app):
    names = [p.name for p in app.snap.projects] if app.snap else []
    return ["ALL"] + names


_RAIL_COL_W = 4                                  # width of each numeric column (3-char header + a pad)
_RAIL_COLS = ("run", "you", "scp", "que", "bkl", "dep")  # running · needs-you · scoping · queued · backlog · awaiting-deploy
_RAIL_MIN_NAME_W = 12                            # below this the table sheds columns (right→left) for the name


def _rail_counts(app, name):
    """(running, needs_you, scoping, queued, backlog, deploy) for one project — or summed for ALL.
    Mirrors the WORK bands: needs_you = founder-gate statuses, scoping = needs_scoping (the lead's
    fill-out queue), queued = the loop's own statuses, backlog = unscheduled, deploy = merged commits
    awaiting deploy."""
    if not app.snap:
        return (0, 0, 0, 0, 0, 0)
    projs = (app.snap.projects if name == "ALL"
             else [p for p in app.snap.projects if p.name == name])
    def total(statuses):
        return sum(len(p.tasks_by_status.get(st, [])) for p in projs for st in statuses)
    run = sum(1 for p in projs if p.running)            # one agent per project, so 0/1 each
    dep = sum(p.deploy_pending or 0 for p in projs)
    return (run, total(d.GATE_ORDER), total(_SCOPING_STATUSES),
            total(_LOOP_STATUSES), total(_BACKLOG_STATUSES), dep)


def render_rail(scr, rect, app, focused):
    """Project navigator AND an at-a-glance table: per-project columns (running · needs-you · queued ·
    backlog) plus an ALL row that totals them — so the founder sees the spread across projects, not
    just one filtered at a time. One uniform color; only a live (running) project is green; the
    needs-you count pops bold yellow; the active filter is bold with a » mark; the focused cursor is
    reverse. Scrolls when more projects than fit (the title badges the hidden count) so the tail is
    never silently truncated."""
    items = _rail_items(app)
    ri = getattr(app, "_rail_i", 0)
    body_h = max(0, rect.h - 2)                          # title row + the column-header row
    base = max(0, ri - body_h + 1) if body_h else 0      # scroll so the selected row stays on-screen
    hidden = max(0, len(items) - body_h)                 # projects off-screen at any scroll position
    title = "PROJECTS" if not hidden else f"PROJECTS  +{hidden}"
    inner = render_pane_title(scr, rect, title, focused)
    # shed columns (right→left) on a narrow rail so the project name always stays readable; real
    # two-column layouts give the rail ≥40 cols, where all columns fit.
    # -4 = a 3-col name indent (mark + dot + a breathing space) + a 1-col right margin (so the
    # rightmost cell isn't clipped by _add, which never writes a row's final column).
    n_cols = max(0, min(len(_RAIL_COLS), (inner.w - 4 - _RAIL_MIN_NAME_W) // _RAIL_COL_W))
    cols = _RAIL_COLS[:n_cols]
    name_w = max(6, inner.w - 4 - n_cols * _RAIL_COL_W)
    cols_x = inner.x + 3 + name_w
    hdr = " " * (3 + name_w) + "".join(f"{c:>{_RAIL_COL_W}}" for c in cols)
    _add(scr, inner.y, inner.x, clip_cols(hdr, inner.w), inner.x + inner.w, curses.A_DIM)
    pf = getattr(app, "project_filter", None)
    for vis_idx, name in enumerate(items[base:base + max(0, inner.h - 1)]):
        idx = base + vis_idx                            # index into items (cursor/active test on this)
        y = inner.y + 1 + vis_idx                       # +1: the column header occupies inner row 0
        active = (name == "ALL" and pf is None) or name == pf
        rev = curses.A_REVERSE if (focused and idx == ri) else 0
        run, you, scp, que, bkl, dep = _rail_counts(app, name)
        live = run > 0 and name != "ALL"                # ALL is an aggregate, never "the live one"
        rowattr = (app._cp(_LIVE) if live else 0) \
            | (curses.A_BOLD if (active and name != "ALL") else 0) | rev
        mark = "\xbb" if active else " "                    # »
        dot = _DOT_RUN if live else " "                 # ● marks the live project (shared with vitals)
        label = f"{mark}{dot} {name}"                    # mark · dot · a space so the ● isn't jammed to the name
        _add(scr, y, inner.x, pad_cols(clip_cols(label, name_w + 3), inner.w),
             inner.x + inner.w, rowattr)                # paint the row full-width so the cursor spans it
        for ci, v in enumerate((run, you, scp, que, bkl, dep)[:n_cols]):
            cell = f"{v:>{_RAIL_COL_W}}" if v else f"{'·':>{_RAIL_COL_W}}"
            if v and ci in (1, 5):                      # needs-you + awaiting-deploy (founder gates) pop bold yellow
                cattr = app._cp(_NEEDS_YOU) | curses.A_BOLD | rev
            elif v and ci == 2:                         # scoping (lead's queue) pops cyan, like the SCOPING band
                cattr = app._cp(_STRUCTURE) | curses.A_BOLD | rev
            elif v:
                cattr = rowattr if live else rev
            else:
                cattr = curses.A_DIM | rev              # a zero is a faint · — present, not shouting
            _add(scr, y, cols_x + ci * _RAIL_COL_W, cell, inner.x + inner.w, cattr)


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
    threads = d.running_threads(app.snap, app._now(), app.root) if app.snap else []
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
            attr = app._log_attr(ln)             # rich fmt-stream coloring (💬/🔧/✓/↳ · errors red)
            _add(scr, by + 1 + i, inner.x, clip_cols("  " + ln, inner.w),
                 inner.x + inner.w, attr)
    if note_n:
        _add(scr, inner.y + inner.h - 1, inner.x,
             clip_cols(f"  +{note_n} more agent(s) - resize to see", inner.w),
             inner.x + inner.w, curses.A_DIM)


def render_runs(scr, rect, app):
    """Full-body RUNS history: the org's completed runs, newest first — one row each
    (HH:MM · project/agent · status · dur · what it touched), scrollable with j/k. Unlike the
    one-line FEED, this keeps task-LESS runs (e.g. a lead planning pass) visible after they end,
    so work that isn't tied to an open task doesn't just vanish."""
    runs = getattr(app, "_runs", None) or []
    h = max(1, rect.h - 1)                         # leave the title row for render_pane_title
    top = max(0, min(getattr(app, "runs_scroll", 0), max(0, len(runs) - h)))
    app.runs_scroll = top
    span = f"  [{top + 1}-{min(len(runs), top + h)}]" if len(runs) > h else ""
    inner = render_pane_title(scr, rect, f"RUNS · {len(runs)}{span}", True)
    if not runs:
        _add(scr, inner.y, inner.x, clip_cols("  (no runs yet)", inner.w),
             inner.x + inner.w, curses.A_DIM)
        return
    for i, r in enumerate(runs[top:top + inner.h]):
        dur = f"{r.dur_min}m" if r.dur_min is not None else "··"
        summ = d.short_summary(r.summary) or ("(running)" if r.status == "running" else "—")
        line = f"  {d.to_local_hhmm(r.started_at):<5}  {r.agent:<24}  {r.status:<11}  {dur:>4}  {summ}"
        _add(scr, inner.y + i, inner.x, clip_cols(line, inner.w),
             inner.x + inner.w, _feed_attr(app, r.status))


def render_bar(scr, rect, app, focus):
    """Contextual action bar (reused) + the panel's global keys."""
    if getattr(app, "show_runs", False):
        _add(scr, rect.y, rect.x, pad_cols(" j/k scroll · r/esc back · q quit", rect.w),
             rect.x + rect.w, curses.A_REVERSE)
        return
    if getattr(app, "show_logwall", False):
        _add(scr, rect.y, rect.x, pad_cols(" L/esc back · q quit", rect.w),
             rect.x + rect.w, curses.A_REVERSE)
        return
    rows = app.left_rows()
    _, sel_row = app._selected(rows)
    acts = app.action_bar(sel_row) if sel_row else ""
    keys = ("w watch · R run · t tick · D deploy · tab · / filter · g expand · L logs · r runs · "
            "? help · q quit")
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
    "  r runs            runs history - every completed run, incl. task-less (j/k scroll, esc back)",
    "",
    "  LOOP / RUN  (act on the selected row's project)",
    "  w                 start / stop the watch loop (whole workspace)",
    "  R                 run a role now (menu) — e.g. the lead, on demand",
    "  t                 tick — run the project's next eligible agent once",
    "  p                 pause / resume the loop",
    "  c                 cancel the project's running agent",
    "  D deploy          run the project's deploy: command (confirms; flags + uses --migrate when a",
    "                    pending migration is involved). Always manual — there is no auto-deploy.",
    "",
    "  ? help            this overlay (any key closes)",
    "  q                 quit (asks to confirm)",
]


def render_overlay(scr, h, w, ov):
    """Centered reverse-video modal showing a captured command's output (a bold title row, then the
    output tailed to fit so the footer stays visible). Lets actions like `ship` run IN the panel
    instead of dropping to the console. Consistent padding: a blank row top + bottom, a 2-space left
    margin on every line (matching the ? help + action menu overlays). Any key dismisses it."""
    title = ov.get("title", "") if ov else ""
    lines = ov.get("lines", []) if ov else []
    body_room = max(1, (h - 2) - 3)                      # screen minus margins, minus blank+title+blank
    body = lines[-body_room:] if len(lines) > body_room else lines
    box = [""] + ["  " + title] + ["  " + ln for ln in body] + [""]
    content_w = max((disp_width(ln) for ln in box), default=0)
    bw = min(max(0, w - 4), content_w + 2)               # +2 right margin; sized to content
    bh = min(max(0, h - 2), len(box))
    y0 = max(0, (h - bh) // 2)
    x0 = max(0, (w - bw) // 2)
    for i in range(bh):
        attr = curses.A_REVERSE | (curses.A_BOLD if i == 1 else 0)   # title row (after the blank top)
        _add(scr, y0 + i, x0, pad_cols(clip_cols(box[i], bw), bw), x0 + bw, attr)


def render_help(scr, h, w):
    """Centered keymap overlay. Consistent padding: a blank row top + bottom, a 2-space left margin
    (carried by the lines), +2 right margin; sized to its widest line so no description is clipped."""
    body = [""] + _HELP_LINES + [""]             # blank top/bottom rows = vertical padding
    content_w = max((disp_width(ln) for ln in body), default=0)
    bw = min(w - 4, content_w + 2)
    bh = min(h - 2, len(body))
    y0 = max(0, (h - bh) // 2)
    x0 = max(0, (w - bw) // 2)
    for i in range(bh):
        line = body[i]
        attr = curses.A_REVERSE | (curses.A_BOLD if line.strip() == "KEYS" else 0)
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
        self.show_runs = False           # full-body RUNS history (the `r` view)
        self.runs_scroll = 0
        self._runs = []
        self.show_overlay = False        # an action's captured output, shown in-panel (e.g. ship)
        self._overlay = None

    def refresh(self):
        super().refresh()
        if self.show_runs:               # keep the open RUNS view live as new runs complete
            try:
                self._runs = d.load_runs(self.conn)
            except d.sqlite3.Error:
                pass                      # keep the last list; the next tick retries

    def left_rows(self):
        rows = panel_work_rows(self.snap, project=self.project_filter,
                               expanded=self._panel_expanded, root=self.root)
        if self.filter:                          # honest filter: narrow to matching task/running rows
            rows = [r for r in rows if r["kind"] in ("task", "running")]
            rows = d.filter_rows(rows, self.filter, key=_row_search_text)
        return rows

    def _capture(self, cmd):
        """Run a non-interactive `dais …` command capturing its output. Returns (rc, text)."""
        r = d.subprocess.run([self._dais()] + [str(c) for c in cmd],
                             capture_output=True, text=True, stdin=d.subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def _dispatch(self, cmd):
        """Override the inherited blocking dispatch to CAPTURE output instead of inheriting this
        terminal — a quick action's stdout (e.g. 'updated win-105') printed over the curses screen
        corrupts the panel. We don't need the text here; the panel redraws + flashes."""
        return self._capture(cmd)[0]

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
        if self.show_runs or self.show_logwall:
            render_vitals(scr, Rect(0, 0, 1, w), self)
            body = render_runs if self.show_runs else render_logwall
            body(scr, Rect(1, 0, max(1, h - 2), w), self)
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
        if self.show_runs:                      # the RUNS history is a scrollable full-body view
            if ch == ord("q"):
                return not self._confirm("quit dais top?")
            if ch in (ord("r"), 27):            # r or esc returns to the control panel
                self.show_runs = False
            elif ch in (ord("j"), curses.KEY_DOWN):
                self.runs_scroll += 1
            elif ch in (ord("k"), curses.KEY_UP):
                self.runs_scroll = max(0, self.runs_scroll - 1)
            return True
        if self.show_logwall:                   # the wall is a passive full-body view
            if ch == ord("q"):
                return not self._confirm("quit dais top?")
            if ch in (ord("L"), 27):            # L or esc returns to the control panel
                self.show_logwall = False
            return True
        if ch == ord("L"):                      # open the live log wall
            self.show_logwall = True
            return True
        if ch == ord("r"):                      # open the RUNS history (completed runs, incl task-less)
            self.show_runs = True
            self.runs_scroll = 0
            try:
                self._runs = d.load_runs(self.conn)
            except d.sqlite3.Error:
                self._runs = []
            return True
        # global panel keys first
        if ch == ord("q"):
            return not self._confirm("quit dais top?")   # confirm before exiting
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
            up = ch in (ord("k"), curses.KEY_UP)
            # the running log is tail-anchored (detail_scroll = rows scrolled UP from the latest):
            # up → into history, down → back toward the live tail. Normal detail scrolls from the top.
            if sel_row and sel_row.get("kind") == "running":
                self.detail_scroll += 1 if up else -1
            else:
                self.detail_scroll += -1 if up else 1
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
# Every status → a ≤7-col uppercase tag so the WORK list's tag column ALWAYS aligns. Raw statuses
# (needs_qa=8, changes_requested=18) overflow the {tag:<7} field and shove the id/project/title right.
_STATUS_TAG = {**GATE_TAG, "needs_scoping": "SCOPE", "ready": "READY", "needs_qa": "QA",
               "changes_requested": "CHANGES", "doing": "RUN", "backlog": "BACK",
               "deferred": "DEFER", "done": "DONE", "cancelled": "CANC"}


def _short_tag(status):
    """A ≤7-col tag for any status (unknown/custom ones are abbreviated) so the tag column aligns."""
    return _STATUS_TAG.get(status) or (status or "").upper().replace("_", "")[:7]
_LOOP_STATUSES = d.LOOP_SUMMARY_ORDER          # ["ready","needs_qa","changes_requested"]
_SCOPING_STATUSES = ["needs_scoping"]          # handed to the lead to flesh out before it's ready
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


def panel_work_rows(snap, *, project=None, expanded=False, root=d.HOME):
    """The panel's WORK list: ordered bands of selectable rows (RUNNING · NEEDS YOU · QUEUED ·
    BACKLOG · DEFERRED · ARCHIVE). `project` limits to one project. `expanded` (the g key) shows
    the full BACKLOG, reveals DEFERRED rows, and uncaps the ARCHIVE. An empty RUNNING/NEEDS YOU/
    QUEUED band collapses to just its dim header (no '(none …)' filler)."""
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
    threads = [t for t in d.running_threads(snap, now, root)
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
        rows.append(_task_row(proj, t, _short_tag(t.status)))

    # AWAITING DEPLOY — merged to main but not yet shipped (deploy-configured projects). A founder
    # action: lists EXACTLY which commits `D` will ship, so you never deploy blind. ⚠ if any carries
    # a migration. Only appears when something is pending (no empty header).
    dep_projs = [p for p in projects if (p.deploy_pending or 0) > 0]
    if dep_projs:
        total = sum(p.deploy_pending or 0 for p in dep_projs)
        mig = any(p.deploy_migration for p in dep_projs)
        add_band("AWAITING DEPLOY" + ("  ⚠ migration" if mig else ""), total)
        shown = 0
        for p in dep_projs:
            for sha7, subj in (p.deploy_commits or []):
                rows.append({"kind": "info", "id": f"__deploy::{p.name}::{sha7}", "sel": False,
                             "label": f"  SHIP    {sha7:<8} {p.name[:11]:<11} {subj}"})
                shown += 1
        if total > shown:
            rows.append({"kind": "info", "id": "__deploy_more", "sel": False,
                         "label": f"  +{total - shown} more  ·  press D to deploy"})

    # SCOPING — sparse tasks handed to the lead to flesh out (founder → `dais handoff <id> lead`);
    # the lead writes a real spec, then promotes to ready (or proposes new direction).
    scoping = tasks_in(_SCOPING_STATUSES)
    add_band("SCOPING", len(scoping))
    for proj, t in scoping:
        rows.append(_task_row(proj, t, "SCOPE"))

    # QUEUED — work cycling through the build/QA loop (ready/needs_qa/changes_requested), waiting for
    # its next turn; shown as rows by default (no g needed). RUNNING above = an agent live right now.
    loop = tasks_in(_LOOP_STATUSES)
    add_band("QUEUED", len(loop))
    for proj, t in loop:
        rows.append(_task_row(proj, t, _short_tag(t.status)))

    # OTHER STAGES — any custom status not covered by a band above (e.g. a project that adds a
    # `needs_design` stage) is auto-surfaced as its own band, so adding a status needs no panel change.
    known = ({"doing"} | set(d.GATE_ORDER) | set(_SCOPING_STATUSES) | set(_LOOP_STATUSES)
             | set(_BACKLOG_STATUSES) | set(_DEFERRED_STATUSES) | set(_ARCHIVE_STATUSES))
    present = {st for p in projects for st, ts in p.tasks_by_status.items() if ts}
    for st in sorted(present - known):
        extra = tasks_in([st])
        if not extra:
            continue
        add_band(st.upper().replace("_", " "), len(extra))
        for proj, t in extra:
            rows.append(_task_row(proj, t, _short_tag(t.status)))

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

    # ARCHIVE — history at the very bottom; shown when a project is picked or expanded; g UNCAPS it.
    # Newest-completed first (by last status change), so the most recent work is at the top.
    if expanded or project is not None:
        arch = sorted(tasks_in(_ARCHIVE_STATUSES),
                      key=lambda pt: pt[1].updated_at or "", reverse=True)
        add_band("ARCHIVE", len(arch))
        shown = arch if expanded else arch[:_ARCHIVE_CAP]    # g shows the full history
        for proj, t in shown:
            tag = "DONE" if t.status == "done" else "CANC"
            rows.append(_task_row(proj, t, tag))
        if len(arch) > len(shown):
            rows.append({"kind": "info", "id": "__arch_more", "sel": False,
                         "label": f"  +{len(arch) - len(shown)} older   (press g to show all)"})
    return rows
