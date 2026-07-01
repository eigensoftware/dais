"""Action primitives shared by the cockpit TUI.

The per-status action catalog (task_actions/action_command) is gone — the TUI now derives a task's
actions from its machine's outgoing edges (dashboard._machine_actions / machine.edge_actions), and
running-row actions are inlined in the App. What remains here is the tiny shared vocabulary:
the Action dataclass and the priority cycle. Stdlib-only.
"""
from dataclasses import dataclass


@dataclass
class Action:
    id: str        # canonical action id (a machine verb, __start, or a metadata op)
    label: str     # human text, e.g. "approve → done"
    key: str       # shortcut char ("a","x","e","h") or "" if menu-only
    slot: str      # "advance" | "reverse" | "menu"
    confirm: bool  # whether the UI must pop a y/N before executing


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
