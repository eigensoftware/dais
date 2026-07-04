# Agent Config Reorganization + Multi-Provider Agents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One file is one agent (frontmatter config in `agents/<role>.md`), machine.json owns `access`, project.yaml slims to project facts, the roles file retires with back-compat — then provider adapters (anthropic/openai) with `auth: subscription|api` and env-only secrets.

**Architecture:** All per-agent resolution centralizes in ONE python function (`router.agent_setup`) exposed to bash via a `--agent-config` CLI mode; run-agent.sh and the dashboard consume it instead of their own parsing. Provider support is a dispatch over per-provider adapter functions in run-agent.sh plus a provider mode in fmt-stream.py. Every legacy location keeps working this release (frontmatter always wins); `dais migrate --config` converts a project mechanically.

**Tech Stack:** bash 3.2, python3 stdlib only (no yaml lib — frontmatter is flat `key: value` lines), sqlite3, unittest.

**Spec:** `docs/superpowers/specs/2026-07-04-agent-config-and-providers-design.md`

## Global Constraints

- Python stdlib only; frontmatter parsed line-based (flat `key: value` between `---` markers), never a YAML library.
- bash 3.2 compatible (macOS default) — no associative arrays, no `${var,,}`.
- Resolution chains (first hit wins), copied from the spec:
  - model/effort: frontmatter → project.yaml `model_<role>`/`effort_<role>` → project.yaml `model`/`effort` → default (`claude-opus-4-8` for anthropic; empty = CLI default for openai)
  - provider: frontmatter → project.yaml → `anthropic`
  - auth: frontmatter → project.yaml → `subscription`
  - access: machine.json roles → legacy roles file col 2 → `review`
  - trigger/prec: frontmatter → legacy roles file → `reactive` / `50`
  - playbook: frontmatter → legacy roles file col 6 → project.yaml `playbook:` → `code`
- Secrets never in config files; `auth: api` reads `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` from process env, `~/.dais/env`, or `$DAIS_HOME/.env` (that precedence: process env wins).
- Full test suite green after every task: `cd harness && python3 -m unittest discover tests`.
- Work in `~/Desktop/dais` (the tool repo). The eigen workspace conversion is Task 13's checklist, not tool code.
- The FINAL commit's message must contain the user migration instructions (Task 12, verbatim text included there).

---

### Task 1: `frontmatter()` parser in router.py

**Files:**
- Modify: `harness/router.py` (add function after `parse_roles`, ~line 38)
- Test: `harness/tests/test_router.py`

**Interfaces:**
- Produces: `frontmatter(path) -> dict[str,str]` — flat keys from a leading `---` block; `{}` on no file / no block / unterminated block. Inline ` #` comments stripped from values.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_router.py`:

```python
class TestFrontmatter(unittest.TestCase):
    """Flat `key: value` lines between leading --- markers of a persona file.
    Line-based on purpose (no YAML library) — nested values are not supported."""
    def _write(self, text):
        d = tempfile.mkdtemp(prefix="dais-fm-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "qa.md")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_reads_flat_keys(self):
        p = self._write("---\nmodel: claude-opus-4-8[1m]\ntrigger: every:5h\nprec: 3\n---\nYou are QA.\n")
        fm = router.frontmatter(p)
        self.assertEqual(fm["model"], "claude-opus-4-8[1m]")
        self.assertEqual(fm["trigger"], "every:5h")   # value itself may contain ':'
        self.assertEqual(fm["prec"], "3")

    def test_inline_comment_stripped(self):
        p = self._write("---\neffort: high   # crank it\n---\nbody\n")
        self.assertEqual(router.frontmatter(p)["effort"], "high")

    def test_no_frontmatter_is_empty(self):
        self.assertEqual(router.frontmatter(self._write("You are QA. No block here.\n")), {})

    def test_unterminated_block_is_empty(self):
        self.assertEqual(router.frontmatter(self._write("---\nmodel: x\nno closing marker\n")), {})

    def test_missing_file_is_empty(self):
        self.assertEqual(router.frontmatter("/nonexistent/qa.md"), {})

    def test_blank_and_comment_lines_ignored(self):
        p = self._write("---\n\n# a comment\nplaybook: plan\n---\nbody\n")
        self.assertEqual(router.frontmatter(p), {"playbook": "plan"})
```

Also add the needed imports at the top of the test file if absent: `import tempfile, shutil` (the file already imports `os`, `unittest`, and `router`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/dais/harness && python3 -m unittest tests.test_router.TestFrontmatter -v`
Expected: ERROR ×6 — `AttributeError: module 'router' has no attribute 'frontmatter'`

- [ ] **Step 3: Implement** — in `harness/router.py`, after `parse_roles` (line ~38):

```python
def frontmatter(path):
    """Flat `key: value` lines between leading --- markers of an agents/<role>.md persona.
    The per-agent config home (model/effort/provider/auth/trigger/prec/playbook). Line-based
    on purpose — no YAML library (stdlib-only harness), no nesting. Returns {} when the file,
    the block, or the closing marker is absent; inline ` #` comments are stripped."""
    fm = {}
    try:
        with open(path) as fh:
            if fh.readline().strip() != "---":
                return {}
            for raw in fh:
                line = raw.strip()
                if line == "---":
                    return fm
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                fm[k.strip()] = v.split(" #", 1)[0].strip()
    except OSError:
        return {}
    return {}          # unterminated block -> treat as no frontmatter
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/dais/harness && python3 -m unittest tests.test_router.TestFrontmatter -v`
Expected: PASS ×6. Then full suite: `python3 -m unittest discover tests` → OK.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/router.py harness/tests/test_router.py
git commit -m "config: frontmatter() — flat key:value block in agents/<role>.md"
```

---

### Task 2: `agent_setup()` resolution + `--agent-config` CLI mode

**Files:**
- Modify: `harness/router.py` (add `agent_setup` after `frontmatter`; extend `__main__`)
- Test: `harness/tests/test_router.py`

**Interfaces:**
- Consumes: `frontmatter(path)` (Task 1), `parse_roles(path)`, `_machine_for(root, project)`, `_playbook_file(root, project, name)` — all already in router.py.
- Produces: `agent_setup(root, project, role) -> dict` with string keys exactly: `model, effort, provider, auth, access, trigger, prec, playbook, playbook_file`. CLI: `router.py --agent-config <root> <project> <role>` prints one `key=value` line per key, in that order.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_router.py`:

```python
class TestAgentSetup(unittest.TestCase):
    """One resolution authority: frontmatter -> legacy roles file -> project.yaml -> defaults;
    access: machine.json roles -> legacy roles file -> review."""
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dais-as-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pdir = os.path.join(self.root, "projects", "demo")
        os.makedirs(os.path.join(self.pdir, "agents"))
        with open(os.path.join(self.pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: demo\nmodel: claude-opus-4-8\neffort: high\n"
                    "model_qa: claude-haiku-4-5\nstage_goal: x\n")
        # a machine whose roles carry access (the new authority)
        with open(os.path.join(self.pdir, "machine.json"), "w") as f:
            f.write('{"name":"t","entry":"ready","roles":{"engineer":{"access":"edit"},'
                    '"qa":{"access":"review"}},'
                    '"states":{"ready":{"initial":true},"done":{"terminal":true}},'
                    '"edges":[{"from":"ready","to":"done","by":"engineer","verb":"finish"}]}')

    def _agent(self, role, fm=""):
        with open(os.path.join(self.pdir, "agents", role + ".md"), "w") as f:
            f.write((("---\n%s---\n" % fm) if fm else "") + "You are %s.\n" % role)

    def test_frontmatter_wins_over_suffix_key(self):
        self._agent("qa", "model: claude-sonnet-5\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual(s["model"], "claude-sonnet-5")

    def test_suffix_key_wins_over_project_default(self):
        self._agent("qa")
        self.assertEqual(router.agent_setup(self.root, "demo", "qa")["model"], "claude-haiku-4-5")

    def test_project_default_then_tool_default(self):
        self._agent("engineer")
        s = router.agent_setup(self.root, "demo", "engineer")
        self.assertEqual(s["model"], "claude-opus-4-8")     # project-wide
        self.assertEqual(s["effort"], "high")

    def test_access_from_machine_roles(self):
        self._agent("engineer")
        self.assertEqual(router.agent_setup(self.root, "demo", "engineer")["access"], "edit")

    def test_access_legacy_roles_file_then_review_default(self):
        self._agent("lead")
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("lead  draft  every:5h  -  3  plan\n")
        s = router.agent_setup(self.root, "demo", "lead")
        self.assertEqual(s["access"], "draft")              # legacy roles file (not in machine)
        self.assertEqual(s["trigger"], "every:5h")
        self.assertEqual(s["prec"], "3")
        self.assertEqual(s["playbook"], "plan")
        os.remove(os.path.join(self.pdir, "roles"))
        s = router.agent_setup(self.root, "demo", "lead")
        self.assertEqual(s["access"], "review")             # safe default
        self.assertEqual(s["trigger"], "reactive")
        self.assertEqual(s["prec"], "50")

    def test_provider_auth_defaults_and_frontmatter(self):
        self._agent("qa", "provider: openai\nauth: api\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertEqual((s["provider"], s["auth"]), ("openai", "api"))
        self._agent("engineer")
        s = router.agent_setup(self.root, "demo", "engineer")
        self.assertEqual((s["provider"], s["auth"]), ("anthropic", "subscription"))

    def test_playbook_file_resolved(self):
        os.makedirs(os.path.join(self.pdir, "playbooks"))
        with open(os.path.join(self.pdir, "playbooks", "design.md"), "w") as f:
            f.write("design conventions\n")
        self._agent("qa", "playbook: design\n")
        s = router.agent_setup(self.root, "demo", "qa")
        self.assertTrue(s["playbook_file"].endswith("projects/demo/playbooks/design.md"))

    def test_cli_mode_prints_key_value_lines(self):
        self._agent("qa", "model: claude-sonnet-5\n")
        import subprocess
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(router.__file__), "router.py"),
                              "--agent-config", self.root, "demo", "qa"],
                             capture_output=True, text=True).stdout
        self.assertIn("model=claude-sonnet-5", out)
        self.assertIn("provider=anthropic", out)
        self.assertIn("access=review", out)
```

Add `import sys` to the test file imports if absent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/dais/harness && python3 -m unittest tests.test_router.TestAgentSetup -v`
Expected: ERROR ×8 — `no attribute 'agent_setup'`

- [ ] **Step 3: Implement** — in `harness/router.py` after `frontmatter()`:

```python
AGENT_CONFIG_KEYS = ("model", "effort", "provider", "auth", "access",
                     "trigger", "prec", "playbook", "playbook_file")


def _yaml_line(text, key):
    """First-line value of `key:` from project.yaml text ('' if absent) — the python twin
    of lib.sh's pcfg, comment-stripped."""
    m = re.search(r"(?m)^%s:[ \t]*(.*)$" % re.escape(key), text)
    return m.group(1).split(" #", 1)[0].strip() if m else ""


def agent_setup(root, project, role):
    """THE resolution authority for how one role runs (spec 2026-07-04): frontmatter ->
    legacy roles file -> project.yaml (suffix key, then project-wide) -> defaults.
    access: machine.json roles -> legacy roles file -> 'review' (safe: runs, read-only).
    Every consumer (run-agent.sh via --agent-config, the scheduler, the dashboard) reads
    through here so the layers can't drift."""
    pdir = os.path.join(root, "projects", project)
    fm = frontmatter(os.path.join(pdir, "agents", role + ".md"))
    legacy = next((r for r in parse_roles(os.path.join(pdir, "roles"))[0]
                   if r["name"] == role), {})
    ytext = ""
    projyaml = os.path.join(pdir, "project.yaml")
    if os.path.exists(projyaml):
        with open(projyaml) as fh:
            ytext = fh.read()

    provider = fm.get("provider") or _yaml_line(ytext, "provider") or "anthropic"
    model = (fm.get("model") or _yaml_line(ytext, "model_" + role)
             or _yaml_line(ytext, "model")
             or ("claude-opus-4-8" if provider == "anthropic" else ""))
    effort = (fm.get("effort") or _yaml_line(ytext, "effort_" + role)
              or _yaml_line(ytext, "effort"))
    auth = fm.get("auth") or _yaml_line(ytext, "auth") or "subscription"

    access = ""
    try:
        import machine as MC
        m = MC.load(_machine_for(root, project))
        access = (m.get("roles", {}).get(role, {}) or {}).get("access", "")
    except Exception:
        pass                                    # machine lint reports its own problems
    access = access or str(legacy.get("access", "")) or "review"

    trigger = fm.get("trigger") or str(legacy.get("trigger", "")) or "reactive"
    prec = fm.get("prec") or (legacy.get("prec_raw", "") if legacy else "") or "50"
    playbook = (fm.get("playbook") or str(legacy.get("playbook", "") if legacy else "")
                or _yaml_line(ytext, "playbook") or "code")
    return {"model": model, "effort": effort, "provider": provider, "auth": auth,
            "access": access, "trigger": trigger, "prec": str(prec),
            "playbook": playbook,
            "playbook_file": _playbook_file(root, project, playbook)}
```

And extend `__main__` (before the `--lint` branch):

```python
    if len(sys.argv) > 1 and sys.argv[1] == "--agent-config":
        s = agent_setup(sys.argv[2], sys.argv[3], sys.argv[4])
        for k in AGENT_CONFIG_KEYS:
            print("%s=%s" % (k, s[k]))
        sys.exit(0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/dais/harness && python3 -m unittest tests.test_router.TestAgentSetup -v` → PASS ×8; full suite OK.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/router.py harness/tests/test_router.py
git commit -m "config: agent_setup() — one resolution authority + --agent-config CLI mode"
```

---

### Task 3: run-agent.sh consumes `--agent-config`

**Files:**
- Modify: `harness/run-agent.sh:16-24` (model/effort), `:77-88` (playbook), `:137-148` (access), `:157` (persona)
- Test: `harness/tests/test_cli.py` (the existing `DAIS_SHOW_CONFIG` tests at ~:409-435 keep passing; add frontmatter-wins + access tests)

**Interfaces:**
- Consumes: `router.py --agent-config <home> <project> <role>` → `key=value` lines (Task 2).
- Produces: run-agent resolves EVERYTHING per-agent through that one call; `DAIS_SHOW_CONFIG=1` prints `model=<m> effort=<e> provider=<p> auth=<a> access=<acc> playbook=<pb>` on one line. The persona's frontmatter block is STRIPPED before injection into the agent prompt.

- [ ] **Step 1: Write the failing tests** — in `harness/tests/test_cli.py`, inside the class holding the existing model-resolution tests (~line 409; it scaffolds a project and runs run-agent with `DAIS_SHOW_CONFIG=1`), add:

```python
    def test_frontmatter_model_beats_suffix_key(self):
        # project.yaml says model_qa: claude-haiku-4-5 (set in setUp/fixture); frontmatter wins
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent) as f:
            body = f.read()
        with open(agent, "w") as f:
            f.write("---\nmodel: claude-sonnet-5\n---\n" + body)
        out = self._show_config("qa")
        self.assertIn("model=claude-sonnet-5", out)

    def test_show_config_includes_provider_auth_access(self):
        out = self._show_config("qa")
        self.assertIn("provider=anthropic", out)
        self.assertIn("auth=subscription", out)
        self.assertIn("access=", out)

    def test_frontmatter_stripped_from_prompt(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nmodel: claude-sonnet-5\n---\nPERSONA-BODY-MARKER\n")
        out = self._show_prompt("qa")          # DAIS_SHOW_PROMPT seam + role file dump
        self.assertNotIn("model: claude-sonnet-5", out)
```

Follow the file's existing helper conventions: if the class has no `_show_config`/`_show_prompt` helpers, mirror how the existing tests at :409-435 invoke run-agent with `DAIS_SHOW_CONFIG=1` / `DAIS_SHOW_PROMPT=1` env and reuse that invocation style. NOTE: `DAIS_SHOW_PROMPT` prints `$STANDING` only — the persona is passed separately via `--append-system-prompt`. For the third test, extend the `DAIS_SHOW_PROMPT` seam in run-agent.sh to also print the stripped persona (see Step 3), so the test can assert on it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/dais/harness && python3 -m unittest tests.test_cli -k frontmatter -v`
Expected: FAIL (frontmatter not honored; `provider=` absent).

- [ ] **Step 3: Implement** — in `harness/run-agent.sh`:

Replace lines 16-24 (the MODEL/EFF/pcfg block and DAIS_SHOW_CONFIG seam):

```bash
# Per-agent config — resolved by the ONE authority (router.agent_setup): frontmatter in
# agents/<role>.md -> legacy roles file -> project.yaml -> defaults. See the 2026-07-04 spec.
CFG="$(python3 "$SELF/router.py" --agent-config "$DAIS_HOME" "$PROJECT" "$AGENT")"
cfg(){ printf '%s\n' "$CFG" | sed -n "s/^$1=//p" | head -1; }
MODEL="$(cfg model)"; EFF="$(cfg effort)"
PROVIDER="$(cfg provider)"; AUTH="$(cfg auth)"
ACCESS="$(cfg access)"; PB="$(cfg playbook)"; PB_FILE="$(cfg playbook_file)"
EFFORT_FLAG=(); [ -n "$EFF" ] && EFFORT_FLAG=(--effort "$EFF")
# Debug seam: print the resolved config and exit WITHOUT calling the provider CLI.
if [ "${DAIS_SHOW_CONFIG:-0}" = 1 ]; then
  echo "model=$MODEL effort=$EFF provider=$PROVIDER auth=$AUTH access=$ACCESS playbook=$PB"; exit 0
fi
```

Replace lines 77-83 (the PB/PB_FILE resolution loop) with nothing — `PB`/`PB_FILE` now come from `cfg` above; keep the `PLAYBOOK=""` assembly block (lines 84-88) unchanged.

Replace lines 143-148 (the ACCESS awk + PERM block) with:

```bash
# Permissions come from the resolved access (machine.json roles -> legacy roles file -> review):
#   edit  -> may modify the repo (bypass).
#   review/draft/none/unknown -> read-only on code (Edit/Write/NotebookEdit hard-disallowed).
PERM=(--permission-mode bypassPermissions)
case "$ACCESS" in
  edit) : ;;
  *)    PERM+=(--disallowedTools Edit Write NotebookEdit) ;;
esac
```

Persona frontmatter stripping — before `run_claude` (near line 150), add:

```bash
# The persona injected into the prompt is the BODY only — frontmatter is config, not prompt.
PERSONA="$(awk 'NR==1 && $0=="---" {infm=1; next} infm && $0=="---" {infm=0; next} !infm' "$ROLE")"
```

…change `--append-system-prompt "$(cat "$ROLE")"` (line 157) to `--append-system-prompt "$PERSONA"`, and extend the `DAIS_SHOW_PROMPT` seam (line 132-135) to also print the persona:

```bash
if [ "${DAIS_SHOW_PROMPT:-0}" = 1 ]; then
  printf '%s\n' "$STANDING"
  printf '%s\n' "$PERSONA"
  exit 0
fi
```

(Move the `PERSONA=` line above this seam so both paths share it.)

- [ ] **Step 4: Run tests** — targeted, then full suite; also `bash -n harness/run-agent.sh`.
Expected: new tests PASS; the pre-existing `model=`/`effort=` assertions at test_cli:426-435 still PASS (assertIn on a longer line).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/run-agent.sh harness/tests/test_cli.py
git commit -m "run-agent: resolve per-agent config through agent_setup; strip persona frontmatter"
```

---

### Task 4: scheduler cast from agents/ + frontmatter

**Files:**
- Modify: `harness/router.py:54-92` (`decide`)
- Test: `harness/tests/test_router.py`

**Interfaces:**
- Produces: `cast(root, project) -> list[dict]` — one entry per role: `{name, trigger, prec:int}`, derived from the union of `agents/*.md` filenames and legacy roles-file rows, each field resolved via `agent_setup`. `decide()` uses it; a project with neither agents/ files nor a roles file is idle.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_router.py` (reuse `TestAgentSetup`'s fixture pattern):

```python
class TestCastFromAgents(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dais-cast-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pdir = os.path.join(self.root, "projects", "demo")
        os.makedirs(os.path.join(self.pdir, "agents"))
        with open(os.path.join(self.pdir, "project.yaml"), "w") as f:
            f.write("project: demo\nrepo: demo\nstage_goal: x\n")

    def _agent(self, role, fm=""):
        with open(os.path.join(self.pdir, "agents", role + ".md"), "w") as f:
            f.write((("---\n%s---\n" % fm) if fm else "") + "persona\n")

    def test_cast_from_agent_files_with_frontmatter(self):
        self._agent("engineer")
        self._agent("lead", "trigger: every:5h\nprec: 3\n")
        c = {r["name"]: r for r in router.cast(self.root, "demo")}
        self.assertEqual(c["engineer"]["trigger"], "reactive")
        self.assertEqual(c["engineer"]["prec"], 50)
        self.assertEqual(c["lead"]["trigger"], "every:5h")
        self.assertEqual(c["lead"]["prec"], 3)

    def test_legacy_roles_file_still_contributes(self):
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("qa  review  reactive  -  1\n")
        names = {r["name"] for r in router.cast(self.root, "demo")}
        self.assertIn("qa", names)

    def test_frontmatter_beats_legacy_row(self):
        self._agent("lead", "trigger: none\n")
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("lead  review  every:5h  -  3\n")
        c = {r["name"]: r for r in router.cast(self.root, "demo")}
        self.assertEqual(c["lead"]["trigger"], "none")

    def test_empty_project_is_empty_cast(self):
        self.assertEqual(router.cast(self.root, "demo"), [])
```

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest tests.test_router.TestCastFromAgents -v` → ERROR ×4 (`no attribute 'cast'`).

- [ ] **Step 3: Implement** — in `harness/router.py`, after `agent_setup`:

```python
def cast(root, project):
    """The project's schedulable cast: the union of agents/<role>.md files and legacy
    roles-file rows, each resolved through agent_setup (so frontmatter wins). The agents/
    directory listing IS the cast in the new world; the roles file contributes during the
    transition only."""
    pdir = os.path.join(root, "projects", project)
    names = []
    adir = os.path.join(pdir, "agents")
    if os.path.isdir(adir):
        names += [f[:-3] for f in sorted(os.listdir(adir)) if f.endswith(".md")]
    for r in parse_roles(os.path.join(pdir, "roles"))[0]:
        if r["name"] not in names:
            names.append(r["name"])
    out = []
    for n in names:
        s = agent_setup(root, project, n)
        out.append({"name": n, "trigger": s["trigger"],
                    "prec": int(s["prec"]) if s["prec"].lstrip("-").isdigit() else 999})
    return out
```

And in `decide()` replace lines 59-61:

```python
    roles = cast(root, project)
    if not roles:
        return None
```

(the rest of `decide()` — `dormant`, the cadence loop over `r["trigger"]`/`r["prec"]` — works unchanged against the new dicts).

- [ ] **Step 4: Run tests** — targeted PASS; full suite OK (existing `test_router` decide tests exercise the legacy path via roles files and must stay green).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/router.py harness/tests/test_router.py
git commit -m "router: the agents/ directory is the cast — roles file contributes as legacy"
```

---

### Task 5: project detection decoupled from the roles file

**Files:**
- Modify: `harness/board.py:~253-255` (`load_snapshot` on_disk test), `harness/router.py:195-197` (lint enumeration)
- Test: `harness/tests/test_dashboard.py`, `harness/tests/test_router.py`

**Interfaces:**
- Produces: a project directory is "configured on disk" iff it has a `project.yaml` (the file lint already requires), everywhere that used to test for a `roles` file.

- [ ] **Step 1: Write the failing tests**

In `harness/tests/test_dashboard.py` (TestDataLayer class):

```python
    def test_snapshot_sees_project_without_roles_file(self):
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "newstyle")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "project.yaml"), "w") as f:
                f.write("project: newstyle\nrepo: x\nstage_goal: g\n")
            snap = d.load_snapshot(_seed(), root=root)
            self.assertIn("newstyle", [p.name for p in snap.projects])
```

In `harness/tests/test_router.py`:

```python
    def test_lint_enumerates_project_without_roles_file(self):
        # (inside a class with a tmp workspace fixture; reuse TestCastFromAgents-style setUp)
        # a project.yaml-only project must be linted, not skipped
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            router.lint(self.root, "")
        self.assertIn("demo", buf.getvalue())
```

(Add `import tempfile` to test_dashboard.py imports if absent.)

- [ ] **Step 2: Run to verify failure** — the snapshot test fails (project invisible without `roles`).

- [ ] **Step 3: Implement**

`harness/board.py` — in `load_snapshot`, change the on_disk comprehension:

```python
    # Projects to render = those configured on disk (a dir under projects/ with a project.yaml —
    # the marker lint requires; the roles file is legacy and optional) UNIONed with any project
    # referenced by a task.
    on_disk = ([d for d in os.listdir(pdir)
                if os.path.exists(os.path.join(pdir, d, "project.yaml"))]
               if os.path.isdir(pdir) else [])
```

`harness/router.py:195-197` — in `lint()`:

```python
        projects = sorted(d for d in os.listdir(pdir)
                          if os.path.exists(os.path.join(pdir, d, "project.yaml")))
```

Then sweep for any other roles-file-keyed detection: `grep -rn "'roles'\|\"roles\"\|/roles" dais harness/*.sh harness/*.py | grep -v playbook` and confirm the remaining hits are the legacy readers (parse_roles, migrate task, render_project — updated in Tasks 7-8), not detection.

- [ ] **Step 4: Run tests** — targeted PASS; full suite OK.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/board.py harness/router.py harness/tests/
git commit -m "config: project.yaml (not the roles file) marks a configured project"
```

---

### Task 6: lint — legacy warnings, orphan agents, secret scan

**Files:**
- Modify: `harness/router.py` (`lint_project`)
- Test: `harness/tests/test_router.py`

**Interfaces:**
- Produces: new warnings — `legacy roles file present…`, `legacy per-role keys in project.yaml…`, `legacy active_agents…`, `agents/<x>.md has no role in the machine…`, `possible secret in <file>…`. `lint_project` no longer returns early when the roles file is absent (it lints the new world).

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_router.py` (tmp-workspace fixture with project.yaml + machine.json as in TestAgentSetup):

```python
    def test_warns_on_legacy_roles_file(self):
        with open(os.path.join(self.pdir, "roles"), "w") as f:
            f.write("qa review reactive - 1\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("legacy roles file" in w for w in warns))

    def test_warns_on_legacy_suffix_keys_and_active_agents(self):
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("model_qa: claude-haiku-4-5\nactive_agents: qa engineer\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("model_qa" in w for w in warns))
        self.assertTrue(any("active_agents" in w for w in warns))

    def test_warns_on_agent_file_with_no_machine_role(self):
        self._agent("ghost")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("ghost" in w and "machine" in w for w in warns))

    def test_warns_on_secret_shaped_value(self):
        self._agent("qa", "model: claude-opus-4-8\n")
        with open(os.path.join(self.pdir, "project.yaml"), "a") as f:
            f.write("api_key: sk-ant-abc123def456ghi789\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("secret" in w.lower() for w in warns))

    def test_no_roles_file_is_not_an_early_return(self):
        # trigger/access sanity now runs off the cast, not the roles file
        self._agent("qa", "trigger: sometimes\n")
        _, warns = router.lint_project(self.root, "demo")
        self.assertTrue(any("trigger" in w for w in warns))
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — rework `lint_project` in `harness/router.py`:

1. Delete the early return at :99-100 (`if not os.path.exists(rolesf): return`). Parse the roles file if present (keep the `problems` loop); afterwards add:

```python
    if os.path.exists(rolesf):
        warnings.append("legacy roles file present — run `dais migrate --config %s` "
                        "(frontmatter in agents/<role>.md is the home now)" % project)
```

2. After the project.yaml checks (:105-119), add:

```python
        for key in re.findall(r"(?m)^((?:model|effort)_[a-z_]+):", text):
            warnings.append("legacy per-role key '%s:' in project.yaml — move it into "
                            "agents/<role>.md frontmatter (dais migrate --config)" % key)
        if re.search(r"(?m)^active_agents:", text):
            warnings.append("legacy active_agents: in project.yaml — the agents/ directory "
                            "is the cast now; delete the key")
        if re.search(r"\bsk-(ant|proj)?-?[A-Za-z0-9_-]{16,}", text):
            warnings.append("possible secret in project.yaml — keys belong in the "
                            "environment (~/.dais/env), never in config files")
```

3. Replace the per-role field-sanity loop (:138-156) to run off `cast(root, project)` + `agent_setup` (trigger/access/playbook checks keep their message text, sourced from the resolved values), and add the frontmatter secret scan + orphan check:

```python
    known_roles = set()
    try:
        import machine as MC
        m = MC.load(_machine_for(root, project))
        known_roles = set(m.get("roles", {})) | {"founder"}
    except Exception:
        m = None
    for r in cast(root, project):
        s = agent_setup(root, project, r["name"])
        if s["access"] not in VALID_ACCESS:
            warnings.append("role '%s': unknown access '%s' (expected %s); treated as read-only"
                            % (r["name"], s["access"], "|".join(sorted(VALID_ACCESS))))
        if not (s["trigger"] in ("reactive", "none") or re.match(r"every:\d+h$", s["trigger"])):
            warnings.append("role '%s': unrecognized trigger '%s' (expected reactive|every:Nh|none); "
                            "never scheduled" % (r["name"], s["trigger"]))
        if s["playbook"] and not s["playbook_file"]:
            warnings.append("role '%s': playbook '%s' has no file (looked in projects/%s/playbooks/ "
                            "and harness/playbooks/); falls back to no conventions"
                            % (r["name"], s["playbook"], project))
        persona = os.path.join(root, "projects", project, "agents", r["name"] + ".md")
        if known_roles and r["name"] not in known_roles and os.path.exists(persona):
            warnings.append("agents/%s.md: role '%s' appears in no machine role — dead cast "
                            "member (typo, or add it to machine.json roles)" % (r["name"], r["name"]))
        fmtext = ""
        if os.path.exists(persona):
            with open(persona) as fh:
                fmtext = fh.read(2000)
        if re.search(r"\bsk-(ant|proj)?-?[A-Za-z0-9_-]{16,}", fmtext):
            warnings.append("possible secret in agents/%s.md — keys belong in the environment "
                            "(~/.dais/env), never in config files" % r["name"])
```

(The existing "machine dispatches role X but persona missing" ERROR at :162-171 stays as-is.)

- [ ] **Step 4: Run tests** — targeted PASS; full suite OK (existing lint tests must stay green — the handles-invariant check at :123-135 only fires when a roles file declares `handles`, which none of the fixtures do).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/router.py harness/tests/test_router.py
git commit -m "lint: warn on legacy config locations, orphan cast members, secret-shaped values"
```

---

### Task 7: `dais migrate --config` — mechanical converter

**Files:**
- Create: `harness/migrate_config.py`
- Modify: `dais` (the `migrate)` case, line ~93)
- Test: `harness/tests/test_migrate_config.py` (new)

**Interfaces:**
- Produces: `python3 migrate_config.py <home> <project>` — writes frontmatter into each agents/<role>.md (trigger/prec/playbook from the roles row; model/effort from project.yaml suffix keys), sets `machine.json` roles access from the roles rows, strips `model_*`/`effort_*`/`active_agents` from project.yaml, deletes the roles file. Idempotent: a project with no roles file prints `nothing to migrate` and exits 0. CLI: `dais migrate --config <project>`.

- [ ] **Step 1: Write the failing tests** — `harness/tests/test_migrate_config.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest tests.test_migrate_config -v` → errors (no module).

- [ ] **Step 3: Implement** — `harness/migrate_config.py`:

```python
#!/usr/bin/env python3
"""migrate_config.py <home> <project> — mechanical one-shot conversion to the 2026-07-04
config layout: roles-file trigger/prec/playbook + project.yaml model_*/effort_* suffix keys
move into agents/<role>.md frontmatter; the roles file's access column moves into
machine.json roles (the new authority); active_agents and the suffix keys are stripped
from project.yaml; the roles file is deleted. Defaults are NOT written (a reactive
trigger / prec 50 / absent playbook stays implicit). Existing frontmatter keys win and
are never clobbered."""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router


def migrate(home, project):
    pdir = os.path.join(home, "projects", project)
    rolesf = os.path.join(pdir, "roles")
    if not os.path.exists(rolesf):
        print("nothing to migrate: no legacy roles file in projects/%s" % project)
        return 0
    rows, problems = router.parse_roles(rolesf)
    for ln, msg, line in problems:
        print("  ! roles:%d %s -> %r (skipped)" % (ln, msg, line))

    projyaml = os.path.join(pdir, "project.yaml")
    ytext = open(projyaml).read() if os.path.exists(projyaml) else ""

    # 1) frontmatter into each persona (skip founder — implicit actor, never scheduled)
    for r in rows:
        if r["name"] == "founder":
            continue
        persona = os.path.join(pdir, "agents", r["name"] + ".md")
        body = open(persona).read() if os.path.exists(persona) else "You are the %s.\n" % r["name"]
        existing = router.frontmatter(persona)
        if existing:                      # strip the old block; merged keys win below
            body = re.sub(r"\A---\n.*?\n---\n", "", body, flags=re.S)
        fm = {}
        mval = router._yaml_line(ytext, "model_" + r["name"])
        eval_ = router._yaml_line(ytext, "effort_" + r["name"])
        if mval:
            fm["model"] = mval
        if eval_:
            fm["effort"] = eval_
        if r["trigger"] and r["trigger"] != "reactive":
            fm["trigger"] = r["trigger"]
        if r["prec_raw"] and r["prec_raw"] not in ("50",):
            fm["prec"] = r["prec_raw"]
        if r["playbook"]:
            fm["playbook"] = r["playbook"]
        fm.update(existing)               # existing frontmatter wins
        if fm:
            block = "---\n" + "".join("%s: %s\n" % kv for kv in fm.items()) + "---\n"
            with open(persona, "w") as f:
                f.write(block + body)
            print("  agents/%s.md: frontmatter {%s}" % (r["name"], ", ".join(sorted(fm))))
        elif not os.path.exists(persona):
            with open(persona, "w") as f:
                f.write(body)

    # 2) access -> machine.json roles (the authority)
    mfile = os.path.join(pdir, "machine.json")
    if os.path.exists(mfile):
        m = json.load(open(mfile))
        roles = m.setdefault("roles", {})
        changed = False
        for r in rows:
            if r["name"] == "founder":
                continue
            cur = roles.setdefault(r["name"], {})
            if cur.get("access") != r["access"]:
                cur["access"] = r["access"]
                changed = True
        if changed:
            with open(mfile, "w") as f:
                json.dump(m, f, indent=2)
                f.write("\n")
            print("  machine.json: roles access written (now authoritative)")

    # 3) strip legacy keys from project.yaml
    if ytext:
        kept = [l for l in ytext.splitlines(True)
                if not re.match(r"^(model|effort)_[a-z_]+:", l)
                and not re.match(r"^active_agents:", l)]
        if kept != ytext.splitlines(True):
            with open(projyaml, "w") as f:
                f.writelines(kept)
            print("  project.yaml: legacy per-role keys / active_agents removed")

    # 4) retire the roles file
    os.remove(rolesf)
    print("  roles file deleted — run: dais lint %s" % project)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: migrate_config.py <home> <project>", file=sys.stderr)
        sys.exit(2)
    sys.exit(migrate(sys.argv[1], sys.argv[2]))
```

And in `dais`, extend the `migrate)` case (line ~93):

```bash
  migrate)
    if [ "${1:-}" = "--config" ]; then
      proj="${2:?usage: dais migrate --config <project>}"
      exec python3 "$DAIS_ROOT/harness/migrate_config.py" "$DAIS_HOME" "$proj"
    fi
    # ... (existing db migration body unchanged)
```

Also add the usage line to the `usage()` heredoc: `dais migrate [--config <project>]     apply DB migrations | convert a project to frontmatter config`.

- [ ] **Step 4: Run tests** — targeted PASS ×4; full suite OK; `bash -n dais`.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/migrate_config.py harness/tests/test_migrate_config.py dais
git commit -m "migrate --config: mechanical conversion to frontmatter agents + machine-owned access"
```

---

### Task 8: templates, `role new`, and the cast display

**Files:**
- Modify: `harness/templates/coding/agents/{lead,engineer,qa}.md`, `harness/templates/marketing/agents/*.md` (add frontmatter); Delete: both templates' `roles` files
- Modify: `dais` (`role new` writes frontmatter, not a roles row; scaffold echo)
- Modify: `harness/dashboard.py` `render_project` (~:1247-1265): cast from `router.cast` + `agent_setup`, drop the roles-file read and `active_agents`
- Test: `harness/tests/test_cli.py` (scaffold), `harness/tests/test_dashboard.py` (render_project)

**Interfaces:**
- Consumes: `router.cast`, `router.agent_setup` (Tasks 2, 4).
- Produces: scaffolded projects have NO roles file; template personas carry frontmatter (`lead`: `trigger: every:24h` + `prec: 30` + `playbook: plan`; `engineer`: `prec: 20`; `qa`: `prec: 10`); `dais project <name>` renders the cast from the new resolution.

- [ ] **Step 1: Write the failing tests**

test_cli.py (scaffold area):

```python
    def test_scaffold_has_no_roles_file_and_frontmatter_agents(self):
        r = dais(self.root, "scaffold", "fresh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pdir = os.path.join(self.root, "projects", "fresh")
        self.assertFalse(os.path.exists(os.path.join(pdir, "roles")))
        lead = open(os.path.join(pdir, "agents", "lead.md")).read()
        self.assertTrue(lead.startswith("---\n"))
        self.assertIn("trigger: every:24h", lead)
```

test_dashboard.py:

```python
    def test_render_project_cast_without_roles_file(self):
        with tempfile.TemporaryDirectory() as root:
            pdir = os.path.join(root, "projects", "p")
            os.makedirs(os.path.join(pdir, "agents"))
            with open(os.path.join(pdir, "project.yaml"), "w") as f:
                f.write("project: p\nrepo: x\nstage_goal: g\nmodel: claude-opus-4-8\n")
            with open(os.path.join(pdir, "agents", "qa.md"), "w") as f:
                f.write("---\nmodel: claude-haiku-4-5\n---\npersona\n")
            out = d.render_project(root, "p", color=False)
            self.assertIn("qa", out)
            self.assertIn("claude-haiku-4-5", out)
```

(Match `render_project`'s real signature when writing the test — check how `main()` calls it, `dashboard.py:1616` area, and mirror that.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

Templates — prepend frontmatter, e.g. `harness/templates/coding/agents/lead.md`:

```markdown
---
trigger: every:24h
prec: 30
playbook: plan
---
# Lead — __PROJECT__
...(existing body unchanged)
```

`engineer.md`: `---\nprec: 20\n---` · `qa.md`: `---\nprec: 10\n---` (reactive is the default — not written). Marketing template equivalently from its current roles rows. Then `git rm harness/templates/coding/roles harness/templates/marketing/roles`.

`render_project` — replace the roles-file read (:1250-1264) with:

```python
    P("")
    P(f"  {c['CB']}cast{c['C0']} {c['CD']}(role · access · trigger · model @ effort · persona){c['C0']}")
    for r in router.cast(root, name):
        s = router.agent_setup(root, name, r["name"])
        persona = ("agents/%s.md" % r["name"]
                   if os.path.exists(os.path.join(pdir, "agents", r["name"] + ".md"))
                   else c['CR'] + "no persona" + c['C0'])
        playbook = f"  {c['CD']}[{s['playbook']}]{c['C0']}" if s["playbook"] != "code" else ""
        P(f"    {r['name']:<10} {s['access']:<7} {s['trigger']:<10} "
          f"{s['model']}{' @ ' + s['effort'] if s['effort'] else ''}  "
          f"{c['CD']}{persona}{c['C0']}{playbook}")
    P(f"    {'founder':<10} {c['CD']}(human — gates ◆){c['C0']}")
```

(dashboard.py already imports `router`.)

`dais role new` — in the generation prompt (dais:~323-397): replace the roles-row instructions with frontmatter: ask Claude to output a fenced block starting `---` with `trigger:`/`prec:`/`playbook:` (+ optional `model:`/`effort:`) followed by the persona body; the write step creates `agents/<name>.md` with that content and NO roles-file append. Read the current block in full before editing (`awk '/^  role\)/,/^  learn\)/' dais`) and keep its confirm flow; change only what's generated and where it's written.

- [ ] **Step 4: Run tests** — targeted PASS; full suite OK; manual: `dais project winterbraid | head -15` still renders (legacy path via roles file).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add -A
git commit -m "templates + role new + project view: the agents/ directory is the cast"
```

---

### Task 9 (phase 2): env sourcing + `auth: api` preflight

**Files:**
- Modify: `harness/run-agent.sh` (after the CFG block from Task 3), `dais` (`init)` gitignore line ~88)
- Test: `harness/tests/test_cli.py`

**Interfaces:**
- Produces: run-agent sources `~/.dais/env` then `$DAIS_HOME/.env` (`set -a`; process env wins because sourcing must NOT override already-set vars — implemented with a keep-existing loop, see below); with `auth=api` and the provider key missing, the run fails fast with a clear message and a `failed` run row. `dais init` writes `.env` into the workspace `.gitignore`.

- [ ] **Step 1: Write the failing tests** — test_cli.py:

```python
    def test_api_auth_without_key_fails_fast(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nauth: api\n---\npersona\n")
        r = self._run_agent("qa", env={"ANTHROPIC_API_KEY": ""})   # ensure absent
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ANTHROPIC_API_KEY", r.stdout + r.stderr)

    def test_env_file_supplies_key(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nauth: api\n---\npersona\n")
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write("ANTHROPIC_API_KEY=sk-test-not-real\n")
        out = self._show_config("qa")               # preflight passes; config seam prints
        self.assertIn("auth=api", out)

    def test_init_gitignores_env(self):
        ws = self._init_ws("dais-env-")
        gi = open(os.path.join(ws, ".gitignore")).read()
        self.assertIn(".env", gi)
```

(`_run_agent` = the file's existing pattern for invoking run-agent.sh directly; keep `DAIS_SHOW_CONFIG=1` OUT of the first test so the preflight actually runs — see Step 3 for ordering.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

run-agent.sh — insert between the CFG block and the `DAIS_SHOW_CONFIG` seam... **ordering decision:** the env sourcing runs BEFORE the seam (so `test_env_file_supplies_key` passes), the key preflight runs AFTER the seam (so `DAIS_SHOW_CONFIG` stays offline-testable) but BEFORE the lock/run-row section:

```bash
# Secrets transport (auth: api): the provider's standard env var, from the process env,
# ~/.dais/env (user-level; keep it chmod 600), or $DAIS_HOME/.env (workspace override,
# gitignored by init) — in that order, FIRST setting wins (process env beats both files).
load_env(){
  local f="$1" line k
  [ -f "$f" ] || return 0
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue;; esac
    k="${line%%=*}"
    [ -n "$k" ] && [ -z "$(eval "printf '%s' \"\${$k:-}\"")" ] && export "$k"="${line#*=}"
  done < "$f"
}
load_env "$HOME/.dais/env"
load_env "$DAIS_HOME/.env"
```

…and after the `DAIS_SHOW_CONFIG` seam:

```bash
if [ "$AUTH" = "api" ]; then
  case "$PROVIDER" in
    anthropic) KEYVAR="ANTHROPIC_API_KEY";;
    openai)    KEYVAR="OPENAI_API_KEY";;
    *)         KEYVAR="";;
  esac
  if [ -n "$KEYVAR" ] && [ -z "$(eval "printf '%s' \"\${$KEYVAR:-}\"")" ]; then
    echo "[$PROJECT/$AGENT] auth: api but \$$KEYVAR is not set — put it in your environment," \
         "~/.dais/env, or $DAIS_HOME/.env"; exit 1
  fi
fi
```

`dais init` — change the gitignore line:

```bash
    [ -f "$target/.gitignore" ] || printf 'dais.db\n*.log\n__pycache__/\n.env\n' > "$target/.gitignore"
    grep -q '^\.env$' "$target/.gitignore" 2>/dev/null || echo '.env' >> "$target/.gitignore"
```

- [ ] **Step 4: Run tests** — targeted PASS; full suite OK; `bash -n` both scripts.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/run-agent.sh dais harness/tests/test_cli.py
git commit -m "providers: env-file secrets transport + auth:api preflight; init gitignores .env"
```

---

### Task 10: provider dispatch in run-agent.sh

**Files:**
- Modify: `harness/run-agent.sh` (`run_claude` → adapters + dispatch, lines ~152-174)
- Test: `harness/tests/test_cli.py`

**Interfaces:**
- Produces: `run_agent_anthropic()` (today's `run_claude` body verbatim, consuming `$PERSONA`), `run_agent_openai()` (stub in this task — real flags in Task 12: body is `echo "openai adapter not yet implemented" >&2; return 1`), and the dispatch:

```bash
run_agent(){
  case "$PROVIDER" in
    anthropic) run_agent_anthropic;;
    openai)    run_agent_openai;;
    *) echo "  ✗ no adapter for provider '$PROVIDER' (known: anthropic, openai)" | tee -a "$LOG"; return 1;;
  esac
}
```

Both `if run_claude` call sites (:168, :172) become `if run_agent`.

- [ ] **Step 1: Write the failing test** — test_cli.py:

```python
    def test_unknown_provider_fails_with_named_error(self):
        agent = os.path.join(self.root, "projects", "demo", "agents", "qa.md")
        with open(agent, "w") as f:
            f.write("---\nprovider: gemini\n---\npersona\n")
        out = self._show_config("qa")
        self.assertIn("provider=gemini", out)      # resolution passes it through
        r = self._run_agent("qa")                  # the run itself fails, named
        self.assertIn("no adapter for provider 'gemini'", r.stdout + r.stderr)
```

- [ ] **Step 2: Run to verify failure.** — the run currently invokes `run_claude` regardless.

- [ ] **Step 3: Implement** as specified in Interfaces (rename + dispatch + stub; keep the pipe through fmt-stream inside `run_agent_anthropic`).

- [ ] **Step 4: Run tests** — targeted PASS; full suite OK. Note the unknown-provider run records status `failed` (the existing `if run_agent … else STATUS=failed` handles it).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/dais && git add harness/run-agent.sh harness/tests/test_cli.py
git commit -m "run-agent: provider adapter dispatch — anthropic verbatim, unknown fails named"
```

---

### Task 11: codex stream discovery + fmt-stream openai mode

**Files:**
- Modify: `harness/fmt-stream.py` (provider arg + openai event mapping)
- Create: `harness/tests/fixtures/codex-exec.jsonl` (captured on the dev machine)
- Test: `harness/tests/test_dashboard.py` (the existing fmt-stream tests live there, ~:156)

**Interfaces:**
- Produces: `fmt-stream.py LOGFILE [--provider openai]` — maps codex `exec --json` JSONL onto the SAME markers claude produces (`  💬 `, `  🔧 `, `     ↳ `, `  ✓ `), so log files and TUI coloring are provider-agnostic. Default provider is anthropic (today's behavior, zero flag = zero change).

- [ ] **Step 1 (discovery, manual on the dev machine):**

```bash
codex --version && codex exec --help | head -40
codex exec --json --skip-git-repo-check "Reply with exactly: hello from codex" \
  | tee ~/Desktop/dais/harness/tests/fixtures/codex-exec.jsonl
```

Inspect the captured events. Expected shape (verify and adjust the mapping in Step 4 to what is ACTUALLY captured): JSONL with event objects carrying a `type` (e.g. `thread.started`, `turn.started`, `item.completed`, `turn.completed`) where `item.completed` items have an `item_type`/`type` of `agent_message` (text), `command_execution` (command + aggregated_output), or `reasoning`. Commit the fixture as captured.

- [ ] **Step 2: Write the failing tests** — test_dashboard.py, next to the existing fmt-stream tests (which shell the script; mirror their invocation style):

```python
    def test_fmt_stream_openai_maps_markers(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "codex-exec.jsonl")
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as lf:
            logpath = lf.name
        self.addCleanup(os.unlink, logpath)
        with open(fixture) as fin:
            subprocess.run([sys.executable, os.path.join(HARNESS, "fmt-stream.py"),
                            logpath, "--provider", "openai"],
                           stdin=fin, capture_output=True, text=True)
        log = open(logpath).read()
        self.assertIn("💬", log)                    # the agent_message mapped
        self.assertIn("✓", log)                     # turn completion mapped

    def test_fmt_stream_default_is_anthropic_unchanged(self):
        # a claude stream-json line still maps (regression: the provider arg is additive)
        line = json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "text", "text": "hi"}]}})
        ...  # same invocation pattern, no --provider flag; assert "💬 hi" in log
```

- [ ] **Step 3: Run to verify failure** — unknown `--provider` arg / no markers.

- [ ] **Step 4: Implement** — in `harness/fmt-stream.py`: parse argv (`LOG = argv[1]`, `PROVIDER = "openai" if "--provider openai" in argv-tail else "anthropic"`), keep the claude branch as-is, add:

```python
def handle_openai(e):
    t = e.get("type", "")
    item = e.get("item", {}) or {}
    it = item.get("item_type") or item.get("type") or ""
    if t == "item.completed" and it == "agent_message":
        emit("  💬 " + brief(item.get("text", ""), 400), "cyan")
    elif t == "item.completed" and it == "command_execution":
        emit("  🔧 shell %s" % brief(item.get("command", "")), "yellow")
        outp = item.get("aggregated_output", "")
        if str(outp).strip():
            emit("     ↳ " + brief(outp, 120), "dim")
    elif t == "item.completed" and it == "reasoning":
        pass                                    # thinking — skip like claude's system noise
    elif t == "turn.completed":
        emit("  ✓ done", "green")
    elif t in ("thread.started", "turn.started", "item.started"):
        pass
    else:
        emit("  " + brief(e, 200))
```

…and route in the main loop: `handle_openai(e) if PROVIDER == "openai" else <existing claude branch>`. **Adjust field names to the captured fixture** — the fixture is the contract, not this sketch.

- [ ] **Step 5: Run tests** — targeted PASS; full suite OK.

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/dais && git add harness/fmt-stream.py harness/tests/
git commit -m "fmt-stream: --provider openai — codex exec --json maps onto the shared markers"
```

---

### Task 12: openai adapter + per-provider limit detection + migration-instructions commit

**Files:**
- Modify: `harness/run-agent.sh` (`run_agent_openai` real body; provider-aware cap line :185), `harness/lib.sh` (`is_capped`)
- Test: `harness/tests/test_cli.py` (is_capped patterns), live smoke (manual)

**Interfaces:**
- Consumes: flags pinned by Task 11's discovery (`codex exec --help`).
- Produces: `run_agent_openai()`; `is_capped <log> [provider]` — anthropic patterns as today, openai adds ChatGPT-plan/rate-limit patterns, both add API-metered patterns (`429`, `insufficient_quota`, `credit balance`).

- [ ] **Step 1: Write the failing tests** — test_cli.py (there are existing is_capped tests — extend beside them):

```python
    def test_is_capped_openai_patterns(self):
        for msg in ("You've hit your usage limit. Try again later.",
                    "Rate limit reached for gpt-5.2",
                    "insufficient_quota: your credit balance is too low"):
            with open(self.log, "w") as f:
                f.write(msg + "\n")
            r = self._lib("is_capped '%s' openai" % self.log)
            self.assertEqual(r.returncode, 0, msg)

    def test_is_capped_anthropic_unchanged(self):
        with open(self.log, "w") as f:
            f.write("You've hit your session limit — resets at 3pm\n")
        self.assertEqual(self._lib("is_capped '%s'" % self.log).returncode, 0)
        self.assertEqual(self._lib("is_capped '%s' anthropic" % self.log).returncode, 0)
```

(`self._lib(cmd)` = `bash -c 'source harness/lib.sh; <cmd>'` with DAIS_HOME set — mirror the file's existing lib.sh test helpers if present, else add this one.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

lib.sh — replace `is_capped`:

```bash
# Did a run die on the provider's usage limit (subscription window, plan rate limit, or —
# under auth:api — a 429/credits error)? $2 = provider (default anthropic). Match only
# genuine limit MESSAGES, not an agent merely discussing rate limits in its reasoning.
is_capped(){
  local pats="you'?ve (hit|reached) your (usage|session|5-?hour|weekly) limit"
  case "${2:-anthropic}" in
    openai) pats="$pats|rate limit reached|you'?ve hit your usage limit" ;;
  esac
  pats="$pats|insufficient_quota|credit balance is too low|error.*429"
  grep -qiE "$pats" "${1:-/dev/null}" 2>/dev/null
}
```

run-agent.sh — `is_capped "$LOG"` (line ~176) becomes `is_capped "$LOG" "$PROVIDER"`; the cap echo (:185) becomes:

```bash
[ "$STATUS" = capped ] && echo "  (hit the $PROVIDER usage limit — back off until it resets)"
```

`run_agent_openai` — real body (flags per Task 11 discovery; this is the expected shape):

```bash
run_agent_openai(){
  # codex has no per-tool disallows: edit -> workspace-write; review/draft roles rely on
  # the persona + machine guards (v1 limitation, documented in the spec). DAIS_HOME is
  # added as a writable root so `dais fire` (coordination writes dais.db) always works.
  local sandbox="workspace-write"
  codex exec --json --skip-git-repo-check --cd "$REPO" \
        ${MODEL:+-m "$MODEL"} \
        ${EFF:+-c model_reasoning_effort="$EFF"} \
        --sandbox "$sandbox" \
        -c 'sandbox_workspace_write.writable_roots=["'"$DAIS_HOME"'"]' \
        "$STANDING

$PERSONA" 2>&1 \
        | python3 -u "$DAIS_ROOT/harness/fmt-stream.py" "$LOG" --provider openai
}
```

- [ ] **Step 4: Run tests + live smoke**

Suite green, then the one real end-to-end (dev machine, codex CLI installed): in a scratch workspace, scaffold a project whose repo is a throwaway dir, set `provider: openai` in `agents/engineer.md`, file a trivial task, `dais start <id>`, and verify: the run streams with markers, the log is written, the run row records `succeeded`, and the agent successfully ran `dais fire` (the db write through the sandbox). Fix flags per reality.

- [ ] **Step 5: Commit — THE MIGRATION-INSTRUCTIONS COMMIT.** This is the commit whose message existing users will read; use exactly this body (update only if implementation details changed):

```bash
cd ~/Desktop/dais && git add -A && git commit -m "$(cat <<'EOF'
providers: per-agent anthropic/openai adapters — run codex agents beside claude

Any agent can now run on a different provider and model: `provider:` +
`model:` in its agents/<role>.md frontmatter (or project.yaml defaults).
`auth: subscription` (default) uses the provider CLI's own login; `auth: api`
is metered and reads ANTHROPIC_API_KEY/OPENAI_API_KEY from your environment,
~/.dais/env (chmod 600), or $DAIS_HOME/.env (gitignored). Limit detection and
log streaming are provider-aware; codex runs get a write sandbox scoped to
the repo + DAIS_HOME (v1: review-role code-edit enforcement is weaker on
openai — see the spec).

MIGRATING AN EXISTING WORKSPACE (from the roles-file layout):
  1. update the tool:            git -C <your dais checkout> pull
  2. convert each project:      dais migrate --config <project>
       - moves roles-file trigger/prec/playbook and project.yaml
         model_<role>/effort_<role> into agents/<role>.md frontmatter
       - writes each role's access into machine.json roles (now the authority)
       - cleans project.yaml (active_agents, suffix keys); deletes the roles file
  3. check:                     dais lint
       (legacy-location warnings mean a project isn't converted yet;
        everything legacy KEEPS WORKING this release — fallbacks retire next)
  4. optional providers:        set provider:/auth: per agent or per project;
                                defaults are anthropic + subscription (no change)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018fnu99uqdmqihGj9JYKaxx
EOF
)"
```

---

### Task 13: docs + eigen workspace conversion

**Files:**
- Modify: `README.md` (config section: frontmatter agents, provider/auth table, secrets; roles file removed from Layout), `design/machine-model.md` (roles section: access authoritative in machine roles)
- Workspace (NOT the tool repo): convert the six eigen projects

- [ ] **Step 1: README** — update the Playbooks/roles example (drop the roles-file table, show frontmatter), the Layout section (remove `roles` from the project listing, add frontmatter mention), add a short **Providers** subsection (provider/auth keys, the env-file secrets rule, the v1 openai sandbox caveat), and the CLI table row for `dais migrate --config`.

- [ ] **Step 2: machine-model.md** — the Roles section gains: "the machine's `roles` values are authoritative for `access` (run-agent enforces from here); scheduling/cadence lives in each agent's frontmatter."

- [ ] **Step 3: Commit docs**

```bash
cd ~/Desktop/dais && git add README.md design/machine-model.md
git commit -m "docs: frontmatter agent config, providers, machine-owned access"
```

- [ ] **Step 4: Convert the eigen workspace** (founder's board — do this with the watch loop paused):

```bash
dais pause
for p in winterbraid counsel-os eigen-legal lyrello puttflow dais-site; do
  dais migrate --config "$p"
done
# also any newer projects: jackwangdotcom, puttflow-web — check: ls ~/Desktop/eigen-software-llc/projects/
dais lint            # expect: clean, no legacy warnings
dais status          # renders
dais resume
```

Commit the workspace changes in the eigen repo (its own commit, not the tool's).

- [ ] **Step 5: Final verification** — full tool suite green; `dais top` opened once against the converted workspace (cast renders, an edge fires); push both repos only when the founder says push.

---

## Self-Review (completed at write time)

- **Spec coverage:** frontmatter home (T1-3) · machine-owned access (T2, T7) · slim project.yaml + retired roles file (T5-8) · back-compat chains (T2, T4) · migrate --config (T7) · lint rules (T6) · templates (T8) · provider adapters + auth + env secrets (T9-12) · per-provider limits (T12) · cast display surfacing (T8; provider/auth shown via agent_setup values) · docs + migration instructions (T12-13). Gap found and covered during planning: project detection was roles-file-keyed (T5) — not in the spec explicitly, but implied by "roles file retires".
- **Placeholder scan:** Task 11/12 codex flags are explicitly pinned by a discovery step against the installed CLI — the fixture is the contract; everything else is concrete code.
- **Type consistency:** `agent_setup` keys (`model, effort, provider, auth, access, trigger, prec, playbook, playbook_file`) match every consumer (cfg() in T3, cast() in T4, lint in T6, render_project in T8, adapters in T10/12).
