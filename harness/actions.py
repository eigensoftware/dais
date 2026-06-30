"""Dais action engine — pure logic shared by the cockpit TUI and `dais actions`.

No DB, no I/O (except the `__main__` lister that prints). This module is a
*contract*: the TUI imports it for keyboard/menu wiring and the CLI shells the
`__main__` lister, so the catalog (ids, keys, slots, confirm flags) and the
argv mappings must stay stable. Keep it stdlib-only.
"""

from dataclasses import dataclass
import re
import sys


@dataclass
class Action:
    id: str       # canonical action id (see catalog)
    label: str    # human text, e.g. "approve → ready"
    key: str      # shortcut char ("a","x","o","+","-") or "" if menu-only
    slot: str     # "advance" | "reverse" | "menu"
    confirm: bool  # whether the UI must pop a y/N before executing


# --- builders ---------------------------------------------------------------

def _adv(action_id, label, confirm=False):
    return Action(action_id, label, "a", "advance", confirm)


def _rev(action_id, label, confirm=False):
    return Action(action_id, label, "x", "reverse", confirm)


# Canonical (label, confirm, key) for menu-slot actions, so an id renders
# identically wherever it appears in the catalog.
_MENU = {
    "set_priority": ("set priority", False, ""),   # priority is on the bar as +/- ; this is exact-pick
    "handoff": ("handoff", False, "h"),
    "edit_title": ("edit title", False, "e"),
    "start": ("start now", False, ""),
    "defer": ("defer → deferred", False, ""),
    "cancel": ("cancel", True, ""),
    "to_ready": ("→ ready", False, ""),
    "open_pr": ("open PR", False, "o"),
    "scope": ("scope (→ needs_scoping)", False, "s"),
}


def _m(action_id):
    label, confirm, key = _MENU[action_id]
    return Action(action_id, label, key, "menu", confirm)


# --- the catalog ------------------------------------------------------------

def task_actions(status, kind="task", has_pr=False):
    """Ordered actions valid for a row: advance, then reverse, then menu items.

    kind ∈ {"task","running","project"}.
    """
    if kind == "project":
        return []
    if kind == "running":
        return [_rev("cancel_run", "cancel run", True), _m("set_priority"),
                _m("handoff")]

    # kind == "task" — dispatch on status
    if status == "proposed":
        return [_adv("approve", "approve → ready"),
                _rev("reject", "reject → cancelled", True),
                _m("set_priority"), _m("defer"), _m("edit_title"), _m("start")]

    if status == "needs_review":
        return [_adv("accept", "accept → done"),
                _rev("request_changes", "request changes"),
                _m("set_priority"), _m("edit_title"), _m("cancel")]

    if status == "ready_to_merge":
        out = []
        if has_pr:
            out.append(_adv("ship", "ship PR", True))
        out.append(_rev("request_changes", "request changes"))
        if has_pr:
            out.append(_m("open_pr"))
        out += [_m("set_priority"), _m("cancel")]
        return out

    # the loop's queued stages — startable on demand: `start` runs whichever role handles the status
    # (ready/changes_requested → engineer, needs_qa → QA). A needs_qa row reaches here only when it's
    # NOT currently running (a live QA run shows in RUNNING with cancel-run); queued, it's startable.
    if status in ("ready", "changes_requested", "needs_qa"):
        return [_adv("start", "start now"),
                _rev("defer", "defer → deferred"),
                _m("set_priority"), _m("handoff"), _m("edit_title"),
                _m("cancel")]

    if status == "backlog":
        return [_adv("promote", "promote → ready"),
                _rev("cancel", "cancel", True),
                _m("scope"), _m("start"), _m("set_priority"), _m("defer"), _m("edit_title")]

    if status == "deferred":
        return [_adv("undefer", "un-defer → backlog"),
                _rev("cancel", "cancel", True),
                _m("to_ready"), _m("set_priority"), _m("edit_title")]

    if status == "blocked":
        return [_adv("unblock", "unblock → ready"),
                _rev("cancel", "cancel", True),
                _m("set_priority"), _m("handoff"), _m("edit_title")]

    if status == "doing":                        # genuinely in-flight (an agent parks it here) — no start
        return [_rev("cancel_run", "cancel run", True),
                _m("set_priority"), _m("handoff")]

    if status in ("done", "cancelled"):
        return [_m("set_priority"), _m("edit_title")]

    # unknown / custom status (e.g. a project-defined needs_design) → no inherent advance/reverse
    # semantics, but keep it ROUTABLE: `handoff` (h) sends it to whatever role should take it next.
    return [_m("handoff"), _m("set_priority"), _m("edit_title")]


# --- argv mapping -----------------------------------------------------------

def _pr_num(pr_url):
    """Trailing number from a .../pull/<n> URL, or None."""
    if not pr_url:
        return None
    m = re.search(r"/pull/(\d+)", str(pr_url))
    return m.group(1) if m else None


def action_command(action_id, task):
    """Return the dais argv (without the leading 'dais') to run an action, or
    None for actions the UI handles interactively / unknown ids.

    task is a Mapping with keys id, project, status, pr_url, priority.
    """
    tid = task.get("id")
    if action_id == "approve":
        return ["approve", tid]
    if action_id in ("reject", "cancel"):
        return ["task", "set", tid, "--status", "cancelled"]
    if action_id == "accept":
        return ["task", "set", tid, "--status", "done"]
    if action_id == "request_changes":
        return ["task", "set", tid, "--status", "changes_requested"]
    if action_id == "ship":
        num = _pr_num(task.get("pr_url"))
        if num is None:
            return None
        return ["ship", task.get("project"), num]
    if action_id == "start":
        return ["start", tid]
    if action_id in ("promote", "to_ready", "unblock"):
        return ["task", "set", tid, "--status", "ready"]
    if action_id == "defer":
        return ["task", "set", tid, "--status", "deferred"]
    if action_id == "undefer":
        return ["task", "set", tid, "--status", "backlog"]
    if action_id == "scope":
        return ["task", "set", tid, "--status", "needs_scoping"]   # whoever owns needs_scoping scopes it
    if action_id == "cancel_run":
        return ["cancel", task.get("project")]
    # set_priority, handoff, edit_title, new, open_pr, unknown → interactive/None
    return None


# --- priority cycle ---------------------------------------------------------

_PRIORITIES = ["low", "medium", "high", "critical"]


def priority_cycle(current, direction):
    """Step priority toward critical (direction > 0) or low (< 0), clamped.
    Unknown/None current is treated as 'medium'."""
    try:
        idx = _PRIORITIES.index(current)
    except (ValueError, TypeError):
        idx = _PRIORITIES.index("medium")
    if direction > 0:
        idx = min(idx + 1, len(_PRIORITIES) - 1)
    elif direction < 0:
        idx = max(idx - 1, 0)
    return _PRIORITIES[idx]


# --- __main__ lister (used by `dais actions`) -------------------------------

_HINTS = {
    "set_priority": "+/- or menu",
    "handoff": "menu: pick a role",
    "edit_title": "menu: edit title",
}


def _render(argv):
    status = argv[0] if len(argv) > 0 else "ready"
    kind = argv[1] if len(argv) > 1 else "task"
    has_pr = (argv[2] == "1") if len(argv) > 2 else False
    task = {
        "id": argv[3] if len(argv) > 3 else "TASK-?",
        "project": argv[4] if len(argv) > 4 else "project",
        "status": status,
        "pr_url": argv[5] if len(argv) > 5 else None,
        "priority": argv[6] if len(argv) > 6 else "medium",
    }
    acts = task_actions(status, kind, has_pr)
    width = max([len(x.label) for x in acts] + [len("new task")])
    lines = []
    for act in acts:
        cmd = action_command(act.id, task)
        if cmd is not None:
            rhs = "dais " + " ".join(str(c) for c in cmd)
        elif act.id == "open_pr":
            rhs = task.get("pr_url") or "(no pr)"
        else:
            rhs = _HINTS.get(act.id, "")
        key = act.key if act.key else " "
        lines.append(f"{key}  {act.label.ljust(width)}  {rhs}".rstrip())
    lines.append(f"n  {'new task'.ljust(width)}".rstrip())
    return "\n".join(lines)


if __name__ == "__main__":
    print(_render(sys.argv[1:]))
