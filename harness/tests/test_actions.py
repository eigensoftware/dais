import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import actions as a  # harness/actions.py


def ids(acts):
    return [x.id for x in acts]


def by_id(acts, action_id):
    for x in acts:
        if x.id == action_id:
            return x
    return None


def by_slot(acts, slot):
    return [x for x in acts if x.slot == slot]


SAMPLE = {
    "id": "cou-9",
    "project": "acme",
    "status": "ready",
    "pr_url": "https://github.com/x/y/pull/42",
    "priority": "medium",
}


class TestActionShape(unittest.TestCase):
    def test_dataclass_fields(self):
        act = a.Action(id="approve", label="approve → ready", key="a",
                       slot="advance", confirm=False)
        self.assertEqual(act.id, "approve")
        self.assertEqual(act.label, "approve → ready")
        self.assertEqual(act.key, "a")
        self.assertEqual(act.slot, "advance")
        self.assertFalse(act.confirm)


class TestKindGating(unittest.TestCase):
    def test_project_kind_empty(self):
        self.assertEqual(a.task_actions("ready", kind="project"), [])

    def test_project_kind_ignores_status(self):
        for s in ("proposed", "doing", "backlog", "done"):
            self.assertEqual(a.task_actions(s, kind="project"), [])

    def test_running_kind(self):
        acts = a.task_actions("doing", kind="running")
        self.assertEqual(ids(acts), ["cancel_run", "set_priority", "handoff"])
        cr = by_id(acts, "cancel_run")
        self.assertEqual(cr.slot, "reverse")
        self.assertEqual(cr.key, "x")
        self.assertTrue(cr.confirm)
        # no advance for a running row
        self.assertEqual(by_slot(acts, "advance"), [])
        self.assertEqual(by_id(acts, "set_priority").key, "")   # priority is +/- on the bar
        self.assertEqual(by_id(acts, "handoff").key, "h")       # handoff has its own key now


class TestSlotKeyInvariants(unittest.TestCase):
    """Across every catalog status, advance->'a', reverse->'x',
    menu->'' (open_pr is the only menu key, 'o')."""

    STATUSES = [
        "proposed", "needs_review", "ready_to_merge", "ready",
        "changes_requested", "backlog", "deferred", "blocked",
        "doing", "needs_qa", "done", "cancelled", "wat",
    ]

    def test_slot_key_alignment(self):
        for s in self.STATUSES:
            for has_pr in (True, False):
                acts = a.task_actions(s, has_pr=has_pr)
                advs = by_slot(acts, "advance")
                revs = by_slot(acts, "reverse")
                self.assertLessEqual(len(advs), 1, s)
                self.assertLessEqual(len(revs), 1, s)
                for adv in advs:
                    self.assertEqual(adv.key, "a", (s, adv.id))
                for rev in revs:
                    self.assertEqual(rev.key, "x", (s, rev.id))
                # menu actions now carry their own letter key (the bar mirrors them); each must be a
                # single char and must not collide with the advance/reverse slot keys.
                _MENU_KEYS = {"open_pr": "o", "handoff": "h", "edit_title": "e", "scope": "s"}
                for m in by_slot(acts, "menu"):
                    self.assertEqual(m.key, _MENU_KEYS.get(m.id, ""), (s, m.id))
                    self.assertNotIn(m.key, ("a", "x"), (s, m.id))   # never shadow advance/reverse


class TestProposed(unittest.TestCase):
    def setUp(self):
        self.acts = a.task_actions("proposed")

    def test_advance_reverse(self):
        adv = by_slot(self.acts, "advance")[0]
        rev = by_slot(self.acts, "reverse")[0]
        self.assertEqual(adv.id, "approve")
        self.assertEqual(adv.label, "approve → ready")
        self.assertEqual(adv.key, "a")
        self.assertFalse(adv.confirm)
        self.assertEqual(rev.id, "reject")
        self.assertEqual(rev.label, "reject → cancelled")
        self.assertEqual(rev.key, "x")
        self.assertTrue(rev.confirm)

    def test_menu_order(self):
        menu = [x.id for x in by_slot(self.acts, "menu")]
        self.assertEqual(menu, ["set_priority", "defer", "edit_title", "start"])

    def test_full_order(self):
        self.assertEqual(
            ids(self.acts),
            ["approve", "reject", "set_priority", "defer", "edit_title", "start"])


class TestNeedsReview(unittest.TestCase):
    def setUp(self):
        self.acts = a.task_actions("needs_review")

    def test_advance_reverse(self):
        adv = by_slot(self.acts, "advance")[0]
        rev = by_slot(self.acts, "reverse")[0]
        self.assertEqual(adv.id, "accept")
        self.assertEqual(adv.label, "accept → done")
        self.assertFalse(adv.confirm)
        self.assertEqual(rev.id, "request_changes")
        self.assertEqual(rev.label, "request changes")
        self.assertFalse(rev.confirm)

    def test_menu(self):
        menu = by_slot(self.acts, "menu")
        self.assertEqual([x.id for x in menu],
                         ["set_priority", "edit_title", "cancel"])
        self.assertTrue(by_id(self.acts, "cancel").confirm)


class TestReadyToMerge(unittest.TestCase):
    def test_with_pr(self):
        acts = a.task_actions("ready_to_merge", has_pr=True)
        self.assertEqual(
            ids(acts),
            ["ship", "request_changes", "open_pr", "set_priority", "cancel"])
        ship = by_id(acts, "ship")
        self.assertEqual(ship.slot, "advance")
        self.assertEqual(ship.key, "a")
        self.assertEqual(ship.label, "ship PR")
        self.assertTrue(ship.confirm)
        rc = by_id(acts, "request_changes")
        self.assertEqual(rc.slot, "reverse")
        self.assertFalse(rc.confirm)
        opr = by_id(acts, "open_pr")
        self.assertEqual(opr.slot, "menu")
        self.assertEqual(opr.key, "o")
        self.assertTrue(by_id(acts, "cancel").confirm)

    def test_without_pr_drops_ship_and_open_pr(self):
        acts = a.task_actions("ready_to_merge", has_pr=False)
        self.assertIsNone(by_id(acts, "ship"))
        self.assertIsNone(by_id(acts, "open_pr"))
        self.assertEqual(
            ids(acts), ["request_changes", "set_priority", "cancel"])
        # no advance remains
        self.assertEqual(by_slot(acts, "advance"), [])


class TestReadyAndChangesRequested(unittest.TestCase):
    def _check(self, status):
        acts = a.task_actions(status)
        self.assertEqual(
            ids(acts),
            ["start", "defer", "set_priority", "handoff", "edit_title", "cancel"])
        adv = by_id(acts, "start")
        self.assertEqual(adv.slot, "advance")
        self.assertEqual(adv.label, "start now")
        self.assertFalse(adv.confirm)
        rev = by_id(acts, "defer")
        self.assertEqual(rev.slot, "reverse")
        self.assertEqual(rev.label, "defer → deferred")
        self.assertFalse(rev.confirm)
        self.assertTrue(by_id(acts, "cancel").confirm)

    def test_ready(self):
        self._check("ready")

    def test_changes_requested(self):
        self._check("changes_requested")


class TestBacklog(unittest.TestCase):
    def setUp(self):
        self.acts = a.task_actions("backlog")

    def test_order(self):
        self.assertEqual(
            ids(self.acts),
            ["promote", "cancel", "scope", "start", "set_priority", "defer", "edit_title"])

    def test_flags(self):
        adv = by_id(self.acts, "promote")
        self.assertEqual(adv.slot, "advance")
        self.assertEqual(adv.label, "promote → ready")
        self.assertFalse(adv.confirm)
        rev = by_id(self.acts, "cancel")
        self.assertEqual(rev.slot, "reverse")
        self.assertEqual(rev.key, "x")
        self.assertTrue(rev.confirm)


class TestDeferred(unittest.TestCase):
    def setUp(self):
        self.acts = a.task_actions("deferred")

    def test_order(self):
        self.assertEqual(
            ids(self.acts),
            ["undefer", "cancel", "to_ready", "set_priority", "edit_title"])

    def test_flags(self):
        adv = by_id(self.acts, "undefer")
        self.assertEqual(adv.label, "un-defer → backlog")
        self.assertFalse(adv.confirm)
        self.assertTrue(by_id(self.acts, "cancel").confirm)
        self.assertEqual(by_id(self.acts, "to_ready").slot, "menu")


class TestBlocked(unittest.TestCase):
    def setUp(self):
        self.acts = a.task_actions("blocked")

    def test_order(self):
        self.assertEqual(
            ids(self.acts),
            ["unblock", "cancel", "set_priority", "handoff", "edit_title"])

    def test_flags(self):
        adv = by_id(self.acts, "unblock")
        self.assertEqual(adv.label, "unblock → ready")
        self.assertFalse(adv.confirm)
        self.assertTrue(by_id(self.acts, "cancel").confirm)


class TestDoingNeedsQaAsTask(unittest.TestCase):
    def test_doing_is_in_flight_no_start(self):
        # doing = an agent parked it while working → only cancel-run, no start
        acts = a.task_actions("doing", kind="task")
        self.assertEqual(ids(acts), ["cancel_run", "set_priority", "handoff"])
        self.assertEqual(by_slot(acts, "advance"), [])

    def test_needs_qa_is_startable(self):
        # a QUEUED needs_qa row (not currently running) is startable — `a start` runs QA
        acts = a.task_actions("needs_qa", kind="task")
        adv = by_slot(acts, "advance")[0]
        self.assertEqual(adv.id, "start")
        self.assertEqual(adv.key, "a")


class TestTerminal(unittest.TestCase):
    def test_done(self):
        self.assertEqual(ids(a.task_actions("done")),
                         ["set_priority", "edit_title"])

    def test_cancelled(self):
        self.assertEqual(ids(a.task_actions("cancelled")),
                         ["set_priority", "edit_title"])


class TestUnknownStatus(unittest.TestCase):
    def test_unknown(self):
        # a custom/unknown status stays routable via handoff (h) — no advance/reverse semantics
        acts = a.task_actions("zzz-not-a-status")
        self.assertEqual(ids(acts), ["handoff", "set_priority", "edit_title"])
        for m in acts:
            self.assertEqual(m.slot, "menu")


class TestActionCommand(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(a.action_command("approve", SAMPLE),
                         ["approve", "cou-9"])

    def test_reject(self):
        self.assertEqual(a.action_command("reject", SAMPLE),
                         ["task", "set", "cou-9", "--status", "cancelled"])

    def test_cancel(self):
        self.assertEqual(a.action_command("cancel", SAMPLE),
                         ["task", "set", "cou-9", "--status", "cancelled"])

    def test_accept(self):
        self.assertEqual(a.action_command("accept", SAMPLE),
                         ["task", "set", "cou-9", "--status", "done"])

    def test_request_changes(self):
        self.assertEqual(a.action_command("request_changes", SAMPLE),
                         ["task", "set", "cou-9", "--status", "changes_requested"])

    def test_ship_parses_pr_number(self):
        t = dict(SAMPLE, pr_url="https://github.com/x/y/pull/42")
        self.assertEqual(a.action_command("ship", t),
                         ["ship", "acme", "42"])

    def test_ship_none_when_no_pr(self):
        self.assertIsNone(a.action_command("ship", dict(SAMPLE, pr_url=None)))

    def test_ship_none_when_garbage_pr(self):
        self.assertIsNone(
            a.action_command("ship", dict(SAMPLE, pr_url="not-a-url")))

    def test_start(self):
        self.assertEqual(a.action_command("start", SAMPLE), ["start", "cou-9"])

    def test_scope_sets_needs_scoping(self):
        # name-agnostic: scope sets the needs_scoping status; whatever role owns it picks it up
        self.assertEqual(a.action_command("scope", SAMPLE),
                         ["task", "set", "cou-9", "--status", "needs_scoping"])

    def test_backlog_offers_scope(self):
        ids = [act.id for act in a.task_actions("backlog")]
        self.assertIn("scope", ids)

    def test_promote_to_ready_unblock(self):
        for aid in ("promote", "to_ready", "unblock"):
            self.assertEqual(a.action_command(aid, SAMPLE),
                             ["task", "set", "cou-9", "--status", "ready"], aid)

    def test_defer(self):
        self.assertEqual(a.action_command("defer", SAMPLE),
                         ["task", "set", "cou-9", "--status", "deferred"])

    def test_undefer(self):
        self.assertEqual(a.action_command("undefer", SAMPLE),
                         ["task", "set", "cou-9", "--status", "backlog"])

    def test_cancel_run(self):
        self.assertEqual(a.action_command("cancel_run", SAMPLE),
                         ["cancel", "acme"])

    def test_interactive_ids_return_none(self):
        for aid in ("set_priority", "handoff", "edit_title", "new", "open_pr"):
            self.assertIsNone(a.action_command(aid, SAMPLE), aid)

    def test_unknown_id_returns_none(self):
        self.assertIsNone(a.action_command("nope", SAMPLE))


class TestPriorityCycle(unittest.TestCase):
    def test_up_chain(self):
        self.assertEqual(a.priority_cycle("low", 1), "medium")
        self.assertEqual(a.priority_cycle("medium", 1), "high")
        self.assertEqual(a.priority_cycle("high", 1), "critical")

    def test_up_clamp(self):
        self.assertEqual(a.priority_cycle("critical", 1), "critical")

    def test_down_chain(self):
        self.assertEqual(a.priority_cycle("critical", -1), "high")
        self.assertEqual(a.priority_cycle("high", -1), "medium")
        self.assertEqual(a.priority_cycle("medium", -1), "low")

    def test_down_clamp(self):
        self.assertEqual(a.priority_cycle("low", -1), "low")

    def test_none_defaults_to_medium_then_bump(self):
        self.assertEqual(a.priority_cycle(None, 1), "high")
        self.assertEqual(a.priority_cycle(None, -1), "low")

    def test_unknown_defaults_to_medium(self):
        self.assertEqual(a.priority_cycle("bogus", 1), "high")
        self.assertEqual(a.priority_cycle("bogus", -1), "low")


class TestMainLister(unittest.TestCase):
    def _run(self, *argv):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [sys.executable, os.path.join(root, "actions.py"), *argv],
            capture_output=True, text=True)

    def test_proposed_lister(self):
        r = self._run("proposed", "task", "0", "cou-9", "acme", "", "high")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("dais approve cou-9", out)
        self.assertIn("reject", out)
        self.assertIn("new task", out)

    def test_ready_to_merge_lister_has_ship(self):
        r = self._run("ready_to_merge", "task", "1", "cou-5a", "acme",
                      "https://github.com/x/y/pull/42", "high")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dais ship acme 42", r.stdout)


if __name__ == "__main__":
    unittest.main()
