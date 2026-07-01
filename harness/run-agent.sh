#!/usr/bin/env bash
# run-agent.sh <project> <agent> — run one agent headless against its backlog.
# Coordination is via the dais CLI + dais.db; the agent does code work in its repo.
set -uo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)"
source "$SELF/lib.sh"

PROJECT="${1:?usage: run-agent.sh <project> <agent>}"
AGENT="${2:?usage: run-agent.sh <project> <agent>}"
QUIET="${DAIS_QUIET:-0}"   # 1 = parallel run: stream to the log only, keep the console uncluttered
PDIR="$DAIS_HOME/projects/$PROJECT"   # workspace DATA (roles, CONTEXT.md, logs) -> DAIS_HOME
ROLE="$PDIR/agents/$AGENT.md"
[ -f "$ROLE" ] || { echo "no role file: $ROLE"; exit 1; }

REPO="$(repo_path "$PROJECT")"
MODEL="$(pcfg "$PROJECT" model)"; MODEL="${MODEL:-claude-opus-4-8}"
# Optional per-project reasoning effort (project.yaml `effort:`). Unset -> CLI default, so other projects are unaffected.
EFFORT_FLAG=(); EFF="$(pcfg "$PROJECT" effort)"; [ -n "$EFF" ] && EFFORT_FLAG=(--effort "$EFF")
[ -d "$REPO" ] || { echo "repo not found: $REPO"; exit 1; }
git -C "$REPO" fetch -q origin 2>/dev/null || true   # always work against current origin
# Keep the local default branch current. The fetch above only moves origin/* refs, but agents read
# local `main` (and branch off it); a stale local main makes a just-merged PR look UNMERGED and tempts
# the agent to stack on a dead branch (the exact bug this guards). Best-effort fast-forward when the
# repo is sitting on the default branch with a clean tree; a no-op when it's on a feature branch or
# dirty, so it never yanks the tree out from under an in-progress checkout.
DEFBR="$(git -C "$REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"; DEFBR="${DEFBR:-main}"
if [ "$(git -C "$REPO" symbolic-ref --quiet --short HEAD 2>/dev/null)" = "$DEFBR" ]; then
  git -C "$REPO" merge --ff-only "origin/$DEFBR" >/dev/null 2>&1 || true
fi

# concurrency guard: skip only if a DIFFERENT live process already holds the lock. The parallel
# dispatcher pre-writes this lock with OUR pid to reserve the slot at launch (before the slow path
# above), so a lock holding our own $$ is that reservation — claim/confirm it, don't skip ourselves.
LOCK="$PDIR/.lock-$AGENT"
if [ -e "$LOCK" ] && [ "$(cat "$LOCK" 2>/dev/null)" != "$$" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "[$PROJECT/$AGENT] already running (pid $(cat "$LOCK")) — skipping"; exit 0
fi
echo $$ > "$LOCK"

mkdir -p "$PDIR/logs"
TS="$(date +%Y%m%d-%H%M%S)"; LOG="$PDIR/logs/$AGENT-$TS.log"
RUNID="$(db "INSERT INTO runs(project,agent,log_path) VALUES('$(sqlesc "$PROJECT")','$(sqlesc "$AGENT")','$(sqlesc "$LOG")'); SELECT last_insert_rowid();")"
# Publish the run id to the agent's environment. The agent coordinates by shelling out to `dais
# task ...`, and those calls inherit DAIS_RUN_ID — so every task the agent creates/changes is
# recorded against THIS run in run_tasks (see link_run_task). Scoped to this process; child `dais`
# invocations inherit it, the founder's own shell never sees it.
export DAIS_RUN_ID="$RUNID"
# Actor identity for machine transitions: `dais fire` attributes an edge to $DAIS_ACTOR (this agent's
# role) unless --by overrides. So an engineer run firing `complete` is recorded as the engineer.
export DAIS_ACTOR="$AGENT"
START_TS="$(db "SELECT datetime('now');")"   # used to summarize what this run changed

# Clean up on ANY exit — including Ctrl-C / watch-stop / sleep-kill. An interrupted run
# is recorded as such (not left dangling as 'running'); its task is untouched, so the next
# tick simply re-runs it. State lives in the db, never in this process.
cleanup(){ rm -f "$LOCK"; [ -n "${RUNID:-}" ] && db "UPDATE runs SET ended_at=datetime('now'), status=CASE WHEN status='running' THEN 'interrupted' ELSE status END WHERE id=$RUNID;" 2>/dev/null; }
trap cleanup EXIT
trap 'echo "  ⏹ interrupted — task left in place, will resume next tick"; exit 130' INT TERM

STAGE_GOAL="$(pcfg "$PROJECT" stage_goal)"
# Workspace context: company-wide rules + founder decisions that apply to EVERY project. Injected
# (by reference, like the project line) ahead of the project context so agents honor it every run.
# Empty when the workspace has no CONTEXT.md, so single-project / bare workspaces are unaffected.
WS_CONTEXT=""
[ -f "$DAIS_HOME/CONTEXT.md" ] && WS_CONTEXT="Workspace context: FIRST read $DAIS_HOME/CONTEXT.md — company-wide rules and founder decisions that apply to EVERY project (honor them). THEN read the project's $PDIR/CONTEXT.md.

"
# Working conventions (playbook): the craft-specific "how work is done here", bound at the ROLE level
# so one harness runs many domains. Resolution — role's 6th `roles` column → project.yaml `playbook:`
# → built-in 'code'; file — explicit path → projects/<proj>/playbooks/<name>.md → harness/playbooks/.
PB="$(awk -v r="$AGENT" '!/^#/ && $1==r {print $6; exit}' "$PDIR/roles" 2>/dev/null)"
[ -n "$PB" ] || PB="$(pcfg "$PROJECT" playbook)"
[ -n "$PB" ] || PB="code"
PB_FILE=""
for cand in "$PB" "$PDIR/playbooks/$PB.md" "$DAIS_ROOT/harness/playbooks/$PB.md"; do
  [ -f "$cand" ] && { PB_FILE="$cand"; break; }
done
PLAYBOOK=""
[ -n "$PB_FILE" ] && PLAYBOOK="

Working conventions ($PB) — how this kind of work is done here:
$(cat "$PB_FILE")"

# Machine coordination: EVERY project runs an authored state machine (project machine.json →
# `machine:` selector → the coding default). The agent advances a task by FIRING an edge
# (dais fire), never by setting a status. The state vocabulary and this role's own edges are
# DERIVED from the machine, so the prompt always matches exactly what the CLI accepts.
MP="$(machine_path "$PROJECT")"
M_STATES="$(DAIS_MP="$MP" python3 -c "
import os, sys; sys.path.insert(0, '$DAIS_ROOT/harness'); import machine as M
print(', '.join(M.load(os.environ['DAIS_MP']).get('states', {})))" 2>/dev/null)"
M_EDGES="$(DAIS_MP="$MP" DAIS_AG="$AGENT" python3 -c "
import os, sys; sys.path.insert(0, '$DAIS_ROOT/harness'); import machine as M
m = M.load(os.environ['DAIS_MP'])
for e in m.get('edges', []):
    if e.get('by') == os.environ['DAIS_AG']:
        g = ('   (guards: ' + ', '.join(e['guards']) + ')') if e.get('guards') else ''
        print('  - task in %s:  dais fire <task-id> %s   -> %s%s' % (e['from'], e['verb'], e['to'], g))" 2>/dev/null)"
[ -n "$M_EDGES" ] || M_EDGES="  (none — this role isn't a machine actor; work the board via 'dais tasks' and propose new work as tasks at the machine's entry state)"
MACHINE_COORD="This project runs an authored task state machine. Task states are: ${M_STATES:-see dais edges}.
Advance a task ONLY by firing one of its edges as your role ('$AGENT') — NEVER set a status directly and NEVER invent one:
  - A task's fireable edges:  $DAIS_ROOT/dais edges <task-id>
  - Fire one:                 $DAIS_ROOT/dais fire <task-id> <verb>   (guards, when the edge needs them: --confirm | --typed <id> | --attest <fact> | --verify <check>)
Your role's own edges in this machine:
$M_EDGES
Do the work your role owns for the task's current state, then fire the edge that hands it to the next role. Effects (spawning follow-up tasks, batching a release) happen automatically when you fire the edge that declares them. A task that spans multiple runs simply stays in its state — the scheduler re-dispatches you next tick, so don't park it anywhere special.

"

STANDING="You are running headless as the **$AGENT** for the '$PROJECT' project.

Stage goal: $STAGE_GOAL

${WS_CONTEXT}Project context + memory: FIRST read $PDIR/CONTEXT.md — the goal, targets/metrics, founder decisions (honor them), and hard-won gotchas. If you discover something durable this run (a decision, a gotcha, a recurring fix), record it with: $DAIS_ROOT/dais learn $PROJECT \"one concise line\".

${MACHINE_COORD}Coordination runs through the dais CLI (at $DAIS_ROOT/dais) backed by a shared SQLite db — that is the single source of truth for what to work on and how to hand off:
  - Your queue:        $DAIS_ROOT/dais tasks $PROJECT --assignee $AGENT
  - All project tasks: $DAIS_ROOT/dais tasks $PROJECT
  - Task metadata:     $DAIS_ROOT/dais task set <id> [--pr <url>] [--notes \"...\"] [--priority <p>]   (metadata ONLY — state changes go through 'dais fire')
  - New task:          $DAIS_ROOT/dais task add $PROJECT \"title\" [--notes \"...\"]   (enters at the machine's entry state)

Do ONE unit of work this run, then stop. Follow your role file exactly, and the working conventions below. Do not start more than one task. When done, fire the edge that hands the work on per your role.${PLAYBOOK}"

# Debug seam: dump the assembled agent prompt and exit, WITHOUT calling claude. Lets tests
# assert prompt wiring (e.g. workspace-context injection) without an end-to-end model run.
if [ "${DAIS_SHOW_PROMPT:-0}" = 1 ]; then
  printf '%s\n' "$STANDING"
  exit 0
fi

# Permissions come from the role's `access` in the project's roles config:
#   edit  -> may modify the repo (bypass).
#   review/draft/none/unknown -> read-only on code (Edit/Write/NotebookEdit hard-disallowed),
#     so reviewers (qa, gc) and content roles structurally can't change the codebase.
# Bash is still broad (full OS isolation is a future step); the universal founder-gate covers
# every outward action. The harness dir is NOT mounted — agents coordinate only via `dais`.
ACCESS="$(awk -v r="$AGENT" '!/^#/ && $1==r {print $2}' "$PDIR/roles" 2>/dev/null | head -1)"
PERM=(--permission-mode bypassPermissions)
case "$ACCESS" in
  edit) : ;;
  *)    PERM+=(--disallowedTools Edit Write NotebookEdit) ;;
esac

cd "$REPO" || { echo "cd failed"; exit 1; }

# fmt-stream writes the PLAIN log file (always) and colors the terminal on its stdout.
# In QUIET mode (parallel runs) we send that terminal stream to /dev/null so N agents don't
# garble the console — the full log file is still written. pipefail keeps claude's exit code.
run_claude(){
  claude -p "$STANDING" \
        --append-system-prompt "$(cat "$ROLE")" \
        --model "$MODEL" \
        ${EFFORT_FLAG[@]+"${EFFORT_FLAG[@]}"} \
        "${PERM[@]}" \
        --add-dir "$REPO" \
        --output-format stream-json --verbose 2>&1 \
        | python3 -u "$DAIS_ROOT/harness/fmt-stream.py" "$LOG"
}

if [ "$QUIET" = 1 ]; then
  echo "  ${CC}${CB}▶ $PROJECT · $AGENT${C0} ${CD}started (parallel) · log: $LOG${C0}"
  if run_claude >/dev/null; then STATUS=succeeded; else STATUS=failed; fi
else
  echo "${CC}${CB}  ▶ $PROJECT · $AGENT${C0}  ${CD}live · full log: $LOG${C0}"
  echo "${CD}  ──────────────────────────────────────────────────────${C0}"
  if run_claude; then STATUS=succeeded; else STATUS=failed; fi
  echo "${CD}  ──────────────────────────────────────────────────────${C0}"
fi
# A capped, empty, or "Execution error" run is NOT success.
if is_capped "$LOG"; then STATUS=capped
elif [ ! -s "$LOG" ] || grep -qiE "^[[:space:]]*execution error[[:space:]]*$" "$LOG"; then STATUS=failed; fi

# Summarize what the run actually changed: tasks it touched during the run, with their new status.
TOUCHED="$(db "SELECT group_concat(id||'→'||status,', ') FROM tasks WHERE project='$(sqlesc "$PROJECT")' AND updated_at >= '$START_TS';")"
[ -z "$TOUCHED" ] && TOUCHED="no task changes"
db "UPDATE runs SET ended_at=datetime('now'), status='$STATUS', summary='$(sqlesc "$TOUCHED")' WHERE id=$RUNID;"
case "$STATUS" in succeeded) sc="$CG";; capped|failed) sc="$CR";; interrupted) sc="$CY";; *) sc="$C0";; esac
echo "  ${sc}${CB}[$STATUS]${C0} $PROJECT/$AGENT ${CD}—${C0} $TOUCHED"
[ "$STATUS" = capped ] && echo "  (hit the subscription cap — back off until the window resets)"
exit 0
