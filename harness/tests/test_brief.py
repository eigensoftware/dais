"""`dais brief <task>` (plan 4.1): the decision packet for a founder gate, on one screen."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import brief    # noqa: E402
import machine as M  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CODING = os.path.join(ROOT, "harness", "machines", "coding.machine.json")


def _ws():
    root = tempfile.mkdtemp(prefix="dais-brief-")
    os.makedirs(os.path.join(root, "projects", "acme"))
    with open(os.path.join(root, "projects", "acme", "project.yaml"), "w") as f:
        f.write("project: acme\nrepo: x\nstage_goal: ship v2\ntask_max_runs: 6\n")
    shutil.copy(CODING, os.path.join(root, "projects", "acme", "machine.json"))
    conn = sqlite3.connect(os.path.join(root, "dais.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT, title TEXT, status TEXT, priority TEXT DEFAULT 'medium',
      assignee TEXT, pr_url TEXT, notes TEXT, created_at TEXT, updated_at TEXT, blocked_on TEXT, parked_from TEXT,
      touches_migrations INTEGER, budget_lifted_at TEXT, state_entered_at TEXT, check_results TEXT, verdict TEXT);
    CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT, task_id TEXT, status TEXT,
      started_at TEXT, ended_at TEXT, input_tokens INTEGER, cost_usd REAL, provider TEXT, model TEXT);
    CREATE TABLE run_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_id TEXT, verb TEXT, at TEXT);
    CREATE TABLE task_links(id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id TEXT, child_id TEXT, rel TEXT, at TEXT);
    """)
    return root, conn


class TestBrief(unittest.TestCase):
    def setUp(self):
        self.root, self.conn = _ws()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        old = os.environ.get("PATH")
        os.environ["PATH"] = "/usr/bin:/bin"                 # no gh: the brief must still render
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def _task(self, tid, title, status, **kw):
        cols = ["id", "project", "title", "status"] + list(kw)
        self.conn.execute("INSERT INTO tasks(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                          [tid, "acme", title, status] + list(kw.values()))
        self.conn.commit()

    def test_release_brief_lists_encompassed_work_with_verdicts_and_checks(self):
        self._task("ac-1", "login button", "approved", pr_url="https://x/pull/7",
                   verdict=json.dumps({"verdict": "pass", "summary": "all green", "checks": ["bun test"], "by": "qa"}),
                   check_results=json.dumps({"tests_pass": {"ok": 1, "at": "2026-09-09 10:00:00", "pr": "https://x/pull/7"}}))
        self._task("ac-2", "settings page", "approved", pr_url="https://x/pull/8", notes="[qa 2026-09-09 09:00] verified by hand")
        self._task("ac-9", "release 2026-09-09", "release_review", touches_migrations=1,
                   state_entered_at="2026-09-09 08:00:00",
                   notes="[engineer 2026-09-09 08:00] merge order: #7 then #8; migration 0042 adds a column")
        self.conn.executemany("INSERT INTO task_links(parent_id,child_id,rel) VALUES(?,?,?)",
                              [("ac-9", "ac-1", "encompasses"), ("ac-9", "ac-2", "encompasses")])
        self.conn.commit()
        out = brief.render(self.conn, self.root, "ac-9", now="2026-09-09 12:00:00")
        self.assertIn("ac-9", out); self.assertIn("release review", out)
        self.assertIn("4h", out)                                  # waiting since
        self.assertIn("ac-1", out); self.assertIn("https://x/pull/7", out)
        self.assertIn("pass", out); self.assertIn("all green", out)
        self.assertIn("tests_pass ✓", out)                        # the recorded check
        self.assertIn("ac-2", out); self.assertIn("no verdict", out)
        self.assertIn("migrations: yes", out)                     # the attest the greenlight will demand
        self.assertIn("merge order", out)                         # the engineer's audit note
        self.assertIn("greenlight", out); self.assertIn("typed_confirm", out)   # what you can fire, and its guards
        self.assertIn("attest:migration_reviewed", out)
        self.assertIn("gh", out.lower())                          # says PR facts need gh

    def test_proposal_brief_shows_spec_spend_and_duplicate_warning(self):
        self._task("ac-3", "Add dark mode", "proposal_review", state_entered_at="2026-09-08 12:00:00",
                   notes="[lead 2026-09-08 12:00] WHAT: dark mode toggle. WHY NOW: users ask weekly.\n\n"
                         "[system 2026-09-08 12:00] possible duplicate of ac-4 'dark mode for settings' (ready)")
        self.conn.execute("INSERT INTO runs(id,project,agent,task_id,status,started_at,input_tokens,cost_usd) "
                          "VALUES(1,'acme','lead','ac-3','succeeded','2026-09-08 11:00:00',120000,1.2)")
        self.conn.execute("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(1,'ac-3','submit')")
        self.conn.commit()
        out = brief.render(self.conn, self.root, "ac-3", now="2026-09-09 12:00:00")
        self.assertIn("proposal review", out); self.assertIn("1d", out)
        self.assertIn("WHY NOW", out)
        self.assertIn("possible duplicate of ac-4", out)
        self.assertIn("1 run", out); self.assertIn("120k", out); self.assertIn("$1.20", out)
        self.assertIn("approve", out); self.assertIn("request_changes", out)
        self.assertIn("spawns", out)                              # approve's effect, said out loud

    def test_escalated_brief_shows_the_bounce_history(self):
        self._task("ac-5", "flaky checkout", "escalated", state_entered_at="2026-09-09 11:00:00",
                   verdict=json.dumps({"verdict": "fail", "summary": "still 500s on submit", "by": "qa", "verb": "fail"}),
                   notes="[qa 2026-09-09 09:00] verdict: fail — cart empty\n\n[qa 2026-09-09 10:00] verdict: fail — 500 on submit\n\n"
                         "[system 2026-09-09 11:00] bounce limit: 'fail' fired 3 times on this task — escalated")
        out = brief.render(self.conn, self.root, "ac-5", now="2026-09-09 12:00:00")
        self.assertIn("escalated", out)
        self.assertIn("bounce limit", out)
        self.assertIn("still 500s", out)
        self.assertIn("resume", out); self.assertIn("note", out)   # resume carries a note guard

    def test_unknown_task(self):
        self.assertIn("no task", brief.render(self.conn, self.root, "nope"))


if __name__ == "__main__":
    unittest.main()
