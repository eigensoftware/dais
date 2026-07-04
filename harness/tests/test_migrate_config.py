import json, os, shutil, subprocess, sys, tempfile, unittest

HARNESS = os.path.join(os.path.dirname(__file__), "..")


def run_migrate(home, project):
    return subprocess.run([sys.executable, os.path.join(HARNESS, "migrate_config.py"),
                           home, project], capture_output=True, text=True)


class TestMigrateConfig(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dais-mig-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pdir = os.path.join(self.root, "projects", "demo")
        os.makedirs(os.path.join(self.pdir, "agents"))
        with open(os.path.join(self.pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: demo\nmodel: claude-opus-4-8\n"
                    "model_qa: claude-haiku-4-5\neffort_qa: low\n"
                    "active_agents: qa engineer\nstage_goal: x\n")
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("# name access trigger handles prec [playbook]\n"
                    "qa        review reactive - 1\n"
                    "engineer  edit   reactive - 2\n"
                    "lead      draft  every:5h - 3 plan\n"
                    "founder   none   none     - 9\n")
        with open(os.path.join(self.pdir, "machine.json"), "w") as f:
            json.dump({"name": "t", "entry": "ready",
                       "roles": {"qa": {}, "engineer": {}, "lead": {}},
                       "states": {"ready": {"initial": True}, "done": {"terminal": True}},
                       "edges": [{"from": "ready", "to": "done", "by": "engineer", "verb": "x"}]},
                      f)
        for role in ("qa", "engineer", "lead"):
            with open(os.path.join(self.pdir, "agents", role + ".md"), "w") as f:
                f.write("You are %s.\n" % role)

    def test_full_conversion(self):
        r = run_migrate(self.root, "demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        qa = open(os.path.join(self.pdir, "agents", "qa.md")).read()
        self.assertTrue(qa.startswith("---\n"))
        self.assertIn("model: claude-haiku-4-5", qa)      # suffix key moved in
        self.assertIn("effort: low", qa)
        self.assertIn("You are qa.", qa)                   # persona preserved below
        lead = open(os.path.join(self.pdir, "agents", "lead.md")).read()
        self.assertIn("trigger: every:5h", lead)
        self.assertIn("prec: 3", lead)
        self.assertIn("playbook: plan", lead)
        m = json.load(open(os.path.join(self.pdir, "machine.json")))
        self.assertEqual(m["roles"]["qa"]["access"], "review")
        self.assertEqual(m["roles"]["engineer"]["access"], "edit")
        y = open(os.path.join(self.pdir, "project.yaml")).read()
        self.assertNotIn("model_qa", y)
        self.assertNotIn("active_agents", y)
        self.assertIn("model: claude-opus-4-8", y)         # project-wide default kept
        self.assertFalse(os.path.exists(os.path.join(self.pdir, "roles")))
        self.assertNotIn("founder", json.dumps(m["roles"]))  # founder row not written anywhere

    def test_reactive_default_trigger_omitted(self):
        run_migrate(self.root, "demo")
        qa = open(os.path.join(self.pdir, "agents", "qa.md")).read()
        self.assertNotIn("trigger: reactive", qa)          # defaults aren't written

    def test_nothing_to_migrate_is_ok(self):
        os.remove(os.path.join(self.pdir, "roles"))
        r = run_migrate(self.root, "demo")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to migrate", r.stdout)

    def test_existing_frontmatter_wins_and_survives(self):
        with open(os.path.join(self.pdir, "agents", "qa.md"), "w") as f:
            f.write("---\nmodel: claude-sonnet-5\n---\nYou are qa.\n")
        run_migrate(self.root, "demo")
        qa = open(os.path.join(self.pdir, "agents", "qa.md")).read()
        self.assertIn("model: claude-sonnet-5", qa)        # not clobbered
        self.assertNotIn("claude-haiku-4-5", qa)

    def test_suffix_key_with_digit_in_role_slug_is_stripped(self):
        # role slugs may contain digits/dots/dashes (e.g. a second qa instance "qa2") — the
        # project.yaml strip regex must match the full role-slug charset, not just [a-z_]+,
        # even when no matching role row exists (the strip is row-independent, pure regex).
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("model_qa2: claude-haiku-4-5\n")
        run_migrate(self.root, "demo")
        y = open(os.path.join(self.pdir, "project.yaml")).read()
        self.assertNotIn("model_qa2", y)

    def test_keyless_frontmatter_block_is_replaced_not_stacked(self):
        with open(os.path.join(self.pdir, "agents", "qa.md"), "w") as f:
            f.write("---\n# just a comment, no keys\n---\nYou are qa.\n")
        run_migrate(self.root, "demo")
        qa = open(os.path.join(self.pdir, "agents", "qa.md")).read()
        self.assertEqual(qa.count("---\n"), 2)          # exactly one block
        self.assertIn("model: claude-haiku-4-5", qa)
        self.assertIn("You are qa.", qa)
        self.assertNotIn("just a comment", qa)


if __name__ == "__main__":
    unittest.main()
