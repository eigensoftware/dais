"""`dais retro` (plan 4.2): the founder's loop, measured from the transition log — gate
decisions and how long they waited, QA pass/fail, bounces, throughput, and which gates are
yolo candidates (approved without changes nearly every time)."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import retro  # noqa: E402

NOW = "2026-09-09 12:00:00"


def _db():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT, title TEXT, status TEXT, priority TEXT);
    CREATE TABLE task_events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, project TEXT, verb TEXT,
      from_state TEXT, to_state TEXT, actor TEXT, at TEXT);
    CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT, task_id TEXT, status TEXT,
      started_at TEXT, input_tokens INTEGER, cost_usd REAL);
    CREATE TABLE run_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_id TEXT, verb TEXT);
    """)
    ev = []
    # wb: 12 proposals reach proposal_review; 11 approved, 1 bounced -> a yolo candidate
    for i in range(12):
        t = "wb-%d" % i
        c.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (t, "wb", "p%d" % i, "done", "medium"))
        ev.append((t, "wb", "submit", "proposed", "proposal_review", "lead", "2026-09-0%d 10:00:00" % (1 + i % 8)))
        verb = "request_changes" if i == 3 else "approve"
        ev.append((t, "wb", verb, "proposal_review", "proposed" if i == 3 else "done", "founder",
                   "2026-09-0%d 12:00:00" % (1 + i % 8)))                       # decided 2h after arriving
    # acme: 4 proposals, 2 bounced -> not a candidate
    for i in range(4):
        t = "ac-%d" % i
        c.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (t, "acme", "a%d" % i, "done", "medium"))
        ev.append((t, "acme", "submit", "proposed", "proposal_review", "lead", "2026-09-05 10:00:00"))
        ev.append((t, "acme", "request_changes" if i < 2 else "approve", "proposal_review",
                   "proposed" if i < 2 else "done", "founder", "2026-09-06 10:00:00"))   # 24h wait
    # QA on wb: 5 pass, 2 fail; wb-1 failed twice (a bounce)
    for t, verb in (("wb-0", "pass"), ("wb-1", "fail"), ("wb-1", "fail"), ("wb-2", "pass"), ("wb-4", "pass"),
                    ("wb-5", "pass"), ("wb-6", "pass")):
        ev.append((t, "wb", verb, "qa_review", "approved" if verb == "pass" else "blocked", "qa", "2026-09-07 09:00:00"))
    # shipped
    ev.append(("wb-rel", "wb", "shipped", "releasing", "done", "engineer", "2026-09-08 09:00:00"))
    ev.append(("wb-rel2", "wb", "shipped", "releasing", "done", "engineer", "2026-07-01 09:00:00"))   # old
    c.executemany("INSERT INTO task_events(task_id,project,verb,from_state,to_state,actor,at) VALUES(?,?,?,?,?,?,?)", ev)
    c.commit()
    return c


class TestRetro(unittest.TestCase):
    def test_gate_decisions_and_wait(self):
        out = retro.report(_db(), now=NOW, since_days=30)
        wb = next(l for l in out.splitlines() if "wb" in l and "proposal_review" in l)
        self.assertIn("12 decisions", wb)
        self.assertIn("92% approved", wb)
        self.assertIn("2h", wb)                                   # median wait
        ac = next(l for l in out.splitlines() if "acme" in l and "proposal_review" in l)
        self.assertIn("50% approved", ac)
        self.assertIn("24h", ac)

    def test_yolo_candidates_are_named(self):
        out = retro.report(_db(), now=NOW, since_days=30)
        self.assertIn("yolo candidate", out)
        self.assertIn("wb: proposal_review", out)
        self.assertNotIn("acme: proposal_review", out.split("yolo candidate")[1])

    def test_qa_rates_and_bounces(self):
        out = retro.report(_db(), now=NOW, since_days=30)
        qa = next(l for l in out.splitlines() if l.strip().startswith("wb") and "pass" in l and "fail" in l)
        self.assertIn("5 pass", qa); self.assertIn("2 fail", qa)
        self.assertIn("wb-1", out)                                 # the bounce
        self.assertIn("2 fails", out)

    def test_throughput_and_since_filter(self):
        out = retro.report(_db(), now=NOW, since_days=30)
        self.assertIn("1 release shipped", out)                    # wb-rel2 is outside the window
        out = retro.report(_db(), now=NOW, since_days=365)
        self.assertIn("2 releases shipped", out)

    def test_no_events_says_migrate(self):
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
        c.executescript("CREATE TABLE tasks(id TEXT, project TEXT, status TEXT);")
        self.assertIn("dais migrate", retro.report(c, now=NOW))


if __name__ == "__main__":
    unittest.main()
