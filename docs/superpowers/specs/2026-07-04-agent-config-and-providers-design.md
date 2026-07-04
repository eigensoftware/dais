# Agent configuration reorganization + multi-provider agents

Status: draft for founder review · 2026-07-04

## Why

One agent's setup is currently smeared across four files, two of which contain dead or
duplicate entries:

| File | Holds today | Problems |
|---|---|---|
| `project.yaml` | repo, github, machine, priority, stage_goal, model/effort + per-role `model_<r>`/`effort_<r>` suffix keys, playbook default, active_agents | per-role config in a flat suffix namespace; `active_agents` duplicates the cast; `github:` read by nothing |
| `roles` | cast, access, trigger, `handles`, prec, playbook | `handles` is a fossil ('-' everywhere); founder row is boilerplate; positional columns don't scale |
| `machine.json` | states/edges/guards/checks + `roles{access, human}` | the role **values** are read by nothing — dead config that looks authoritative; access duplicated with the roles file |
| `agents/<role>.md` | persona prompt | the only per-agent file, and it holds no config |

Separately, dais is hard-coded to one provider (the `claude` CLI). Supporting OpenAI's
codex CLI — and per-agent provider choice — widens adoption ("bring the AI subscription
you already have") and is the first real test of extensibility.

## Design principle

Group every fact by what kind of fact it is:

- **Contract** (the shareable org design) → `machine.json`
- **Per-agent runtime** (how THIS deployment runs a role) → `agents/<role>.md` frontmatter
- **Project-level deployment facts** → `project.yaml`
- **Secrets** → environment only, never config files

## Target layout (phase 1)

### machine.json — the contract, now including authoritative access

The `roles` property's values become live: `access` is read by run-agent from the machine,
not the roles file. A machine file is then fully self-describing (states, edges, guards,
who exists, what each may do).

```json
"roles": {
  "lead":     {"access": "draft"},
  "engineer": {"access": "edit"},
  "qa":       {"access": "review"}
}
```

`founder` need not be declared (implicit actor); declaring it is allowed and ignored for
dispatch. Access vocabulary unchanged: `edit` · `review` · `draft` · `none`.

### agents/<role>.md — one file IS one agent

YAML frontmatter (flat `key: value` lines between `---` markers — parsed line-based,
stdlib-only, same discipline as `pcfg`) above the persona:

```markdown
---
model: claude-opus-4-8[1m]   # optional; falls back to project.yaml model:
effort: high                 # optional; falls back to project.yaml effort:
provider: anthropic          # phase 2; default anthropic
auth: subscription           # phase 2; subscription | api, default subscription
trigger: every:5h            # cadence: reactive | every:Nh | none (was roles file)
prec: 3                      # dispatch tie-break among cadence roles (was roles file)
playbook: plan               # craft conventions (was roles 6th column)
---
You are the lead for this project…
```

The `agents/` directory listing is the cast. Lint reconciles it against the machine:
- a machine role that agent-dispatches (non-implicit `by:` on any edge) with no
  `agents/<role>.md` → **error**
- an agent file whose role appears in no machine → **warning**

### project.yaml — project-level facts only

```yaml
project: winterbraid
repo: ~/Desktop/winterbraid
github: jw2856/winterbraid       # convention/docs; not read by the tool
machine: coding                  # optional selector when no local machine.json
priority: 1                      # cross-project dispatch order
stage_goal: …
model: claude-opus-4-8[1m]       # project-wide defaults an agent file may override
effort: high
playbook: code                   # project-wide playbook default
provider: anthropic              # phase 2: project-wide provider default
auth: subscription               # phase 2: project-wide auth default
```

Retired keys: `model_<role>` / `effort_<role>` (→ frontmatter), `active_agents`
(→ the agents/ directory + machine roles).

### roles file — retired

Its four live jobs all move: access → machine.json, trigger/prec/playbook → frontmatter,
cast list → agents/ + machine roles. The founder row disappears (implicit).

## Resolution chains

For each per-agent setting, first hit wins:

```
model/effort:      frontmatter → project.yaml → tool default (claude-opus-4-8 / CLI default)
provider:          frontmatter → project.yaml → anthropic
auth:              frontmatter → project.yaml → subscription
access:            machine.json roles → (legacy: roles file column) → review (safe: runs, read-only on code — matches today's treatment of unknown access)
trigger/prec:      frontmatter → (legacy: roles file) → reactive / 50
playbook:          frontmatter → (legacy: roles file col 6) → project.yaml playbook: → code
```

Legacy locations keep working through the transition (see Migration); frontmatter always
wins over legacy.

## Phase 2: providers

### Adapter contract

`run_claude()` in run-agent.sh generalizes to one adapter function per provider, each fed
the same inputs: assembled prompt, role persona, model, effort, access level, auth mode,
repo path, log path.

- **anthropic** → `claude -p` (today's invocation verbatim)
- **openai** → `codex exec` with `--cd <repo>`, `-m <model>`, reasoning effort via config
  flag, persona concatenated into the prompt (no `--append-system-prompt` equivalent),
  `--json` output. Exact sandbox/approval flags pinned against the installed codex
  version during implementation.
- unknown provider → clear error (`no adapter for provider 'x'`) — the seam a future
  pack fills.

Per-provider pieces, one each:
- **stream formatter**: `fmt-stream.py` gains a provider mode mapping codex JSONL events
  onto the same 💬/🔧/✓ markers, so logs/TUI coloring work unchanged.
- **limit detection**: `is_capped` becomes per-provider patterns — Claude plan messages,
  ChatGPT/codex rate-limit messages, plus API-style 429/credit errors when `auth: api`.
  Run status stays `capped`; only the human-facing message generalizes.
- **default model**: anthropic `claude-opus-4-8`; openai: the codex CLI's default.

### Access mapping honesty

anthropic maps `review`/`draft` to `--disallowedTools Edit Write NotebookEdit` (as today).
codex has sandbox levels, not tool disallows, and a fully read-only sandbox would block
`dais fire` (coordination writes dais.db). v1: `edit` → write sandbox scoped to repo +
DAIS_HOME; `review`/`draft` → the tightest sandbox that still permits DAIS_HOME writes.
Net: code-edit enforcement on openai v1 is weaker than on anthropic. Documented, not
hidden.

### auth + secrets

- `auth: subscription` (default): the provider CLI's own login store; no secrets touch
  dais at all.
- `auth: api`: metered; the provider's standard env var must be present
  (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`).
- Key transport: run-agent sources `~/.dais/env` (user-level, must be 0600), then
  `$DAIS_HOME/.env` (workspace-level override), inherited process env winning over both.
  `dais init` gitignores `.env` in the workspace.
- Keys never live in project.yaml/frontmatter; lint warns on key-shaped values
  (`sk-ant-`, `sk-proj-`, …) in config files.
- The panel/status surface shows the resolved provider+auth per role (cast display), and
  a run launched with `auth: api` notes "metered" — the Fable-5-drained-the-credits
  incident is the motivating case.

## Migration

1. Phase 1 lands with full back-compat (legacy resolution chains above). No workspace
   breaks on upgrade.
2. `dais lint` warns on legacy locations (roles file present, suffix keys, active_agents).
3. `dais migrate --config <project>` mechanically converts one project: frontmatter
   written into agents/*.md, machine roles made authoritative, project.yaml cleaned,
   roles file deleted.
4. Scaffold templates + the six eigen workspace projects convert immediately.
5. The roles-file reader and suffix-key fallbacks retire one release later.

## Out of scope (flagged, deliberate)

- Folding machine.json's role `access` further (e.g. per-edge tool policies).
- Provider adapters as installable packs — designed-for but parked with the plugin work.
- Panel/TUI extension points.
- OS-level agent isolation (unchanged from today).

## Testing

- Frontmatter parsing: unit tests (empty, no frontmatter, unknown keys ignored, malformed
  markers).
- Resolution chains: extend the offline `DAIS_SHOW_CONFIG` seam to print
  model/effort/provider/auth/access/trigger/playbook per role; test_cli asserts each
  chain including legacy fallbacks.
- Lint: new rules (cast reconciliation, legacy-location warnings, key-shaped values).
- fmt-stream codex mode: fixture-based (recorded JSONL → expected markers).
- Migration: `dais migrate --config` on a fixture project, byte-compare expected output.
- One live end-to-end codex run on the dev machine (codex CLI already installed).
- No live-API calls in the suite.
