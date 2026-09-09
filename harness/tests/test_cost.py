"""`dais cost` — the run ledger's report (harness/cost.py). Tokens are the primary unit (they
compare across providers); dollars appear where the provider reported them (claude only)."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cost  # noqa: E402

SCHEMA = """
CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT, title TEXT, status TEXT,
  priority TEXT, assignee TEXT, notes TEXT, updated_at TEXT);
CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT, task_id TEXT,
  status TEXT, summary TEXT, log_path TEXT, started_at TEXT, ended_at TEXT, model TEXT,
  provider TEXT, account TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, cost_usd REAL, turns INTEGER, session_id TEXT);
CREATE TABLE run_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_id TEXT,
  verb TEXT, at TEXT);
"""

NOW = "2026-09-08 12:00:00"


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.executemany("INSERT INTO tasks(id,project,title,status) VALUES(?,?,?,?)",
                  [("wb-1", "wb", "ship the thing", "done"),
                   ("wb-2", "wb", "other thing", "ready"),
                   ("ac-1", "acme", "acme work", "done")])
    rows = [
        # id, project, agent, status, started_at, provider, in, out, cache_read, cache_write, cost, turns
        (1, "wb", "engineer", "succeeded", "2026-09-08 10:00:00", "anthropic", 100000, 5000, 80000, 15000, 1.50, 20),
        (2, "wb", "qa", "succeeded", "2026-09-08 10:30:00", "anthropic", 50000, 1000, 45000, 4000, 0.50, 10),
        (3, "wb", "lead", "succeeded", "2026-09-08 11:00:00", "anthropic", 30000, 200, 29000, 500, 0.25, 4),   # no-op
        (4, "acme", "engineer", "succeeded", "2026-09-08 11:30:00", "openai", 20000, 800, 12000, 0, None, 1),
        (5, "wb", "engineer", "failed", "2026-09-01 10:00:00", "anthropic", 10000, 100, 0, 0, 0.10, 2),       # 7 days ago
        (6, "wb", "qa", "interrupted", "2026-09-08 11:45:00", "anthropic", None, None, None, None, None, None),  # no report
    ]
    c.executemany("INSERT INTO runs(id,project,agent,status,started_at,provider,input_tokens,output_tokens,"
                  "cache_read_tokens,cache_write_tokens,cost_usd,turns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.executemany("INSERT INTO run_tasks(run_id,task_id,verb) VALUES(?,?,?)",
                  [(1, "wb-1", "claim"), (1, "wb-1", "complete"), (2, "wb-1", "pass"),
                   (3, "wb-2", "touch"),                       # notes only = no-op
                   (4, "ac-1", "claim"), (5, "wb-1", "claim")])
    c.commit()
    return c


class TestCostReport(unittest.TestCase):
    def test_totals_line_counts_runs_and_tokens(self):
        out = cost.report(_db(), now=NOW)
        self.assertIn("6 runs", out)
        self.assertIn("210k in", out)          # 100k+50k+30k+20k+10k; run 6 reported nothing
        self.assertIn("7.1k out", out)
        self.assertIn("$2.35", out)            # claude runs only; codex has no dollar figure

    def test_by_project_is_the_default_and_names_cached_share(self):
        out = cost.report(_db(), now=NOW)
        wb = next(l for l in out.splitlines() if l.strip().startswith("wb "))
        self.assertIn("5", wb.split()[1])      # runs
        self.assertIn("190k", wb)              # tokens in
        self.assertIn("81%", wb)               # (80k+45k+29k+0)/190k cached
        self.assertIn("$2.35", wb)

    def test_by_role_rows_and_noop_share(self):
        out = cost.report(_db(), by="role", now=NOW)
        lead = next(l for l in out.splitlines() if "wb/lead" in l)
        self.assertIn("1 no-op", lead)         # the notes-only run: touch verbs don't count
        eng = next(l for l in out.splitlines() if "wb/engineer" in l)
        self.assertIn("0 no-op", eng)

    def test_codex_rows_show_no_dollar_figure(self):
        out = cost.report(_db(), by="role", now=NOW)
        acme = next(l for l in out.splitlines() if "acme/engineer" in l)
        self.assertIn("20k", acme)
        self.assertNotIn("$", acme)

    def test_by_task_sums_the_runs_that_touched_it(self):
        out = cost.report(_db(), by="task", now=NOW)
        wb1 = next(l for l in out.splitlines() if l.strip().startswith("wb-1"))
        self.assertIn("ship the thing", wb1)
        self.assertIn("3 runs", wb1)           # runs 1, 2, 5 (run 1 counted once despite two verbs)
        self.assertIn("160k", wb1)
        self.assertIn("$2.10", wb1)

    def test_since_and_project_filters(self):
        out = cost.report(_db(), project="wb", since_days=1, now=NOW)
        self.assertIn("4 runs", out)           # run 5 (7d ago) and acme's run 4 excluded
        self.assertNotIn("acme", out)

    def test_db_without_the_ledger_columns_says_migrate(self):
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
        c.executescript("CREATE TABLE runs(id INTEGER PRIMARY KEY, project TEXT, agent TEXT, status TEXT, "
                        "started_at TEXT); CREATE TABLE tasks(id TEXT, project TEXT, title TEXT, status TEXT);")
        out = cost.report(c, now=NOW)
        self.assertIn("dais migrate", out)

    def test_runs_that_reported_nothing_are_counted_but_flagged(self):
        out = cost.report(_db(), now=NOW)
        self.assertIn("1 run reported no usage", out)


if __name__ == "__main__":
    unittest.main()


class TestCostByAccount(unittest.TestCase):
    """5.4: `--by account` groups on runs.account; a NULL account is the provider's implicit one."""
    def test_by_account_groups_and_defaults_null_to_the_provider(self):
        c = _db()
        c.execute("UPDATE runs SET account='max-a' WHERE id IN (1,2)")
        c.execute("UPDATE runs SET account='max-b' WHERE id=3")
        out = cost.report(c, by="account", now=NOW)
        rows = {l.split()[0]: l for l in out.splitlines() if l.startswith("  ") and not l.strip().startswith("account")}
        self.assertIn("max-a", rows); self.assertIn("max-b", rows)
        self.assertIn("2", rows["max-a"].split()[1])
        self.assertIn("anthropic", rows)           # runs 4, 5 (NULL account, claude) read as the implicit account
        self.assertIn("openai", rows)              # the codex run
        self.assertIn("account", out.splitlines()[2])   # the header names the column

    def test_by_account_on_a_db_without_the_column_says_migrate(self):
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
        c.executescript(SCHEMA.replace(", account TEXT", ""))
        self.assertIn("migrate", cost.report(c, by="account", now=NOW))
