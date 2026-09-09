"""`dais web` (plan 4.5): a localhost, token-gated page over the same data layer as the panel,
with actions that go through `dais fire` (the engine enforces every guard)."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_cli import make_sandbox, dais, q, _PRISTINE   # noqa: E402


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_sandbox()
        for name, data in _PRISTINE.items():
            with open(os.path.join(cls.root, name), "wb") as fh:
                fh.write(data)
        dais(cls.root, "scaffold", "demo")
        dais(cls.root, "task", "add", "demo", "Approve me", "--id", "d-1", "--status", "proposal_review",
             "--notes", "WHAT: a thing. WHY NOW: because.")
        dais(cls.root, "task", "add", "demo", "Build it", "--id", "d-2", "--status", "ready")
        sys.path.insert(0, os.path.join(cls.root, "harness"))
        import importlib
        if "web" in sys.modules:
            del sys.modules["web"]
        cls.web = importlib.import_module("web")
        cls.token = "t0ken"
        cls.server = cls.web.make_server(cls.root, "127.0.0.1", 0, cls.token)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)

    def _url(self, path, token=None):
        return "http://127.0.0.1:%d/%s%s" % (self.port, token or self.token, path)

    def _get(self, path, token=None, raw=False):
        with urllib.request.urlopen(self._url(path, token), timeout=10) as r:
            body = r.read().decode()
            return body if raw else json.loads(body)

    def _post(self, path, payload):
        req = urllib.request.Request(self._url(path), data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_wrong_token_is_404_everywhere(self):
        for path in ("/", "/api/snapshot", "/api/brief/d-1"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self._get(path, token="nope")
            self.assertEqual(cm.exception.code, 404)

    def test_page_has_theme_toggle_defaulting_to_system(self):
        html = self._get("/", raw=True)
        self.assertIn("<html", html)
        self.assertIn("prefers-color-scheme", html)
        self.assertIn("data-theme", html)
        self.assertIn("localStorage", html)
        self.assertIn('id="theme"', html)

    def test_snapshot_json_carries_the_board(self):
        s = self._get("/api/snapshot")
        p = {x["name"]: x for x in s["projects"]}["demo"]
        ids = {t["id"] for ts in p["tasks_by_status"].values() for t in ts}
        self.assertIn("d-1", ids); self.assertIn("d-2", ids)
        self.assertIn("proposal_review", p["bands"]["NEEDS YOU"])
        self.assertIn("ready", p["bands"]["QUEUED"])
        self.assertIn("machine", p); self.assertIn("edges", p["machine"])
        self.assertIn("gates", s); self.assertIn("ts", s)

    def test_edges_mirror_the_engines_prompts(self):
        e = self._get("/api/edges/d-1")
        approve = next(x for x in e["edges"] if x["verb"] == "approve")
        self.assertEqual(approve["by"], "founder")
        self.assertEqual([p["kind"] for p in approve["prompts"]], ["confirm"])
        self.assertTrue(any(x["verb"] == "request_changes" for x in e["edges"]))

    def test_fire_without_the_guard_is_refused_and_state_unchanged(self):
        code, r = self._post("/api/fire", {"task": "d-1", "verb": "approve"})
        self.assertEqual(code, 409, r)
        self.assertIn("confirm", r["error"])
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-1'")[0], "proposal_review")

    def test_fire_with_the_guard_moves_the_task_and_spawns(self):
        dais(self.root, "task", "add", "demo", "Approve me too", "--id", "d-3", "--status", "proposal_review")
        code, r = self._post("/api/fire", {"task": "d-3", "verb": "approve", "confirm": True})
        self.assertEqual(code, 200, r)
        self.assertIn("spawned", r["output"])
        self.assertEqual(q(self.root, "SELECT status FROM tasks WHERE id='d-3'")[0], "done")

    def test_note_and_priority_go_through_task_set(self):
        code, r = self._post("/api/task", {"task": "d-2", "notes": "from the web"})
        self.assertEqual(code, 200, r)
        self.assertIn("from the web", q(self.root, "SELECT notes FROM tasks WHERE id='d-2'")[0])
        code, r = self._post("/api/task", {"task": "d-2", "priority": "high"})
        self.assertEqual(code, 200, r)
        self.assertEqual(q(self.root, "SELECT priority FROM tasks WHERE id='d-2'")[0], "high")
        code, r = self._post("/api/task", {"task": "d-2", "priority": "urgent"})
        self.assertEqual(code, 400)

    def test_brief_and_reports(self):
        b = self._get("/api/brief/d-1", raw=True)
        self.assertIn("WHY NOW", b); self.assertIn("approve", b)
        self.assertIn("dais cost", self._get("/api/cost", raw=True))
        self.assertIn("dais retro", self._get("/api/retro", raw=True))

    def test_machine_endpoint_has_states_edges_and_counts(self):
        m = self._get("/api/machine/demo")
        self.assertIn("proposal_review", m["states"])
        self.assertTrue(any(e["verb"] == "claim" for e in m["edges"]))
        self.assertGreaterEqual(m["counts"].get("ready", 0), 1)

    def test_sse_sends_a_snapshot_frame(self):
        req = urllib.request.Request(self._url("/api/events"))
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn("text/event-stream", r.headers.get("Content-Type", ""))
            line = r.readline().decode()
            while line.strip() == "":
                line = r.readline().decode()
            self.assertTrue(line.startswith("data: "), line)
            self.assertIn("projects", json.loads(line[6:]))

    def test_bad_json_and_unknown_route(self):
        req = urllib.request.Request(self._url("/api/fire"), data=b"{not json", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/api/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
