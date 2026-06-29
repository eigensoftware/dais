#!/usr/bin/env bash
# dispatch.sh [project] [--dry-run]
# One tick of the adaptive scheduler: if there's capacity and nothing already
# running, pick the single most valuable pending action and run it.
# Wire this to a frequent schedule (e.g. every 30 min overnight). The DB state
# decides what runs — not a fixed clock.
set -uo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)"; source "$SELF/lib.sh"

DRY=0; PROJECT=""
for a in "$@"; do [ "$a" = "--dry-run" ] && DRY=1 || PROJECT="$a"; done

# --- reconcile orphaned state (self-heal across stops/starts): drop dead lock files, and if
#     nothing is actually running, mark any leftover 'running' row interrupted AND rewind its
#     task to a re-pickable status. A builder parks a task in 'doing' while working, but NO role
#     `handles` 'doing' (see each project's roles file) — so without this rewind an interrupted
#     task strands there forever and never resumes, despite the stop() banner promising it would.
#     'ready' is re-picked by the engineer (handles ready,changes_requested), so the work resumes.
#     Safe because this only fires when live=0 (no agent holds a lock → nothing is mid-`doing`). ---
if [ "$DRY" = 0 ]; then
  live=0
  for lk in "$DAIS_HOME"/projects/*/.lock-*; do
    [ -e "$lk" ] || continue
    if kill -0 "$(cat "$lk" 2>/dev/null)" 2>/dev/null; then live=1; else rm -f "$lk"; fi
  done
  if [ "$live" = 0 ]; then
    db "UPDATE runs SET status='interrupted', ended_at=datetime('now') WHERE status='running';"
    db "UPDATE tasks SET status='ready', updated_at=datetime('now') WHERE status='doing';"
  fi
fi

# --- pause sentinel: founder parked the loop (dais pause / dais top). Idle, don't launch.
#     One check here makes pause honored by every dispatcher: watch, a hand-run tick, and the
#     launchd schedule. ---
if [ "$DRY" = 0 ] && [ -f "$DAIS_HOME/projects/.paused" ]; then
  echo "${CY}tick: paused (projects/.paused) — run 'dais resume' to continue${C0}"; exit 10
fi

# --- auto-merge pass: for projects with `automerge: true` (project.yaml), ship QA-approved PRs
#     WITHOUT a founder click. SAFE only where merge != deploy (cedar is manual-deploy, so
#     merging never goes live; the founder still gates the deploy). THREE guards keep it honest:
#       1. main CI RED → PAUSE this project's auto-merge until main is green again (don't pile onto a
#          broken main; the net for the A+B break two independently-green PRs can cause). Self-resumes.
#       2. the PR's OWN checks must be GREEN — failing → leave for founder; still running → wait and
#          retry. Without this, "auto-merge" would ship red code just because it's git-mergeable.
#       3. a PR touching a migration (db/migrations or supabase/migrations) is LEFT for the founder.
#     Runs BEFORE the cap/backoff gates: merging is gh/git, not a subscription run, so the founder's
#     merge queue keeps draining even when agent runs are capped. ---
if [ "$DRY" = 0 ]; then
  if [ -n "$PROJECT" ]; then am_projects=("$PROJECT")
  else am_projects=(); for p in "$DAIS_HOME"/projects/*/; do [ -d "$p" ] && am_projects+=("$(basename "$p")"); done; fi
  for amp in "${am_projects[@]}"; do
    [ "$(pcfg "$amp" automerge)" = "true" ] || continue
    amgh="$(pcfg "$amp" github)"; [ -n "$amgh" ] || continue
    # guard 1 — latest main CI run: pause this project's auto-merge while main is RED (fail-safe net).
    ammain="$(gh run list --repo "$amgh" --branch main --limit 1 --json conclusion,status --jq '.[0] | (.conclusion // .status // "")' 2>/dev/null | tr 'a-z' 'A-Z')"
    case "$ammain" in
      FAILURE|CANCELLED|TIMED_OUT|ACTION_REQUIRED|STARTUP_FAILURE)
        echo "${CR}⛔ automerge[$amp]: main CI is ${ammain} — PAUSING auto-merge until main is green (fix main first)${C0}"
        continue ;;
    esac
    while IFS='|' read -r amtid ampurl; do
      [ -z "$amtid" ] && continue
      ampr="${ampurl##*/}"; [[ "$ampr" =~ ^[0-9]+$ ]] || continue
      amms="$(gh pr view "$ampr" --repo "$amgh" --json mergeable --jq .mergeable 2>/dev/null)"
      [ "$amms" = "MERGEABLE" ] || continue   # CONFLICTING/UNKNOWN → leave for founder / retry next tick
      # guard 2 — only auto-merge a PR whose checks are GREEN (red→founder, pending→wait next tick).
      amci="$(gh pr view "$ampr" --repo "$amgh" --json statusCheckRollup --jq '
        [ .statusCheckRollup[]? | (.conclusion // .state // .status // "PENDING") | ascii_upcase ] as $s
        | (["FAILURE","ERROR","CANCELLED","TIMED_OUT","ACTION_REQUIRED","STARTUP_FAILURE"]) as $red
        | (["PENDING","EXPECTED","QUEUED","IN_PROGRESS","WAITING","REQUESTED"]) as $wait
        | if ($s|length)==0 then "none"
          elif ([$s[]|select(. as $x|$red|index($x))]|length)>0 then "red"
          elif ([$s[]|select(. as $x|$wait|index($x))]|length)>0 then "pending"
          else "green" end' 2>/dev/null)"
      case "$amci" in
        green) : ;;
        red)   echo "${CY}automerge[$amp]: PR #$ampr has FAILING checks — left ready_to_merge for the founder${C0}"; continue ;;
        none)  echo "${CD}automerge[$amp]: PR #$ampr has no CI checks yet — waiting for CI (or founder merges manually)${C0}"; continue ;;
        *)     echo "${CD}automerge[$amp]: PR #$ampr checks still running — waiting, retry next tick${C0}"; continue ;;
      esac
      ammigs="$(gh pr diff "$ampr" --repo "$amgh" --name-only 2>/dev/null | grep -E 'migrations/.*\.sql$' || true)"
      if [ -n "$ammigs" ]; then
        echo "${CY}automerge[$amp]: PR #$ampr touches a migration — left ready_to_merge for the founder${C0}"
        continue
      fi
      echo "${CG}${CB}▸ automerge[$amp]: shipping QA-approved + CI-green PR #$ampr (no founder click; merge≠deploy)${C0}"
      "$DAIS_ROOT/dais" ship "$amp" "$ampr" || echo "${CY}automerge[$amp]: ship of #$ampr did not complete — left for founder${C0}"
    done < <(db "SELECT id||'|'||COALESCE(pr_url,'') FROM tasks WHERE project='$(sqlesc "$amp")' AND status='ready_to_merge' AND pr_url IS NOT NULL AND pr_url<>'';")
  done
fi

# --- capacity gate: cool down after a recent cap hit (window resets every ~5h) ---
capped_recent="$(db "SELECT COUNT(*) FROM runs WHERE status='capped' AND started_at > datetime('now','-90 minutes');")"
if [ "${capped_recent:-0}" -gt 0 ]; then
  echo "${CY}tick: hit the subscription cap within 90 min — cooling down until the window frees up${C0}"; exit 20
fi

# --- error backoff: don't spin on a persistent failure (Execution error, transient outage) ---
fail_recent="$(db "SELECT COUNT(*) FROM runs WHERE status='failed' AND started_at > datetime('now','-30 minutes');")"
if [ "${fail_recent:-0}" -ge 2 ]; then
  echo "${CY}tick: 2+ failed runs in last 30 min — backing off (check the latest log; will retry later)${C0}"; exit 20
fi

# --- parallel width: how many agents may run at once (default 1 = serial, today's behavior).
#     Set by `dais watch <interval> <N>` via DAIS_MAX_PARALLEL; clamped to 1..5. ---
MAX="${DAIS_MAX_PARALLEL:-1}"
[[ "$MAX" =~ ^[0-9]+$ ]] || MAX=1
[ "$MAX" -lt 1 ] && MAX=1
[ "$MAX" -gt 5 ] && MAX=5

# how many agents are live right now (across all projects) → how many slots are free this tick
running=0
for lk in "$DAIS_HOME"/projects/*/.lock-*; do
  [ -e "$lk" ] || continue
  kill -0 "$(cat "$lk" 2>/dev/null)" 2>/dev/null && running=$((running+1))
done
free=$((MAX - running))

# which projects to consider
projects=()
if [ -n "$PROJECT" ]; then projects=("$PROJECT")
else for p in "$DAIS_HOME"/projects/*/; do [ -d "$p" ] && projects+=("$(basename "$p")"); done; fi

# Build the eligible set: each NOT-busy project the router wants to run, as one line
# `priority|last_run|project|agent`. At most one agent per project — the per-repo lock forbids
# stacking two agents in the same repo. priority comes from project.yaml (default 100); last_run
# is the project's most recent run start ('' = never run) for least-recently-run fairness.
eligible=()
for proj in "${projects[@]}"; do
  [ "$free" -le 0 ] && break   # pool full — no slot to fill, so don't bother polling the router
  busy=0
  for lk in "$DAIS_HOME/projects/$proj"/.lock-*; do
    [ -e "$lk" ] || continue
    kill -0 "$(cat "$lk" 2>/dev/null)" 2>/dev/null && busy=1
  done
  [ "$busy" = 1 ] && continue   # already running its one agent — counted in `running` above

  # who runs next is decided by the project's roles config (see harness/router.py) — no
  # role names hardcoded here. The router returns a role name to run, or nothing (idle).
  agent="$(python3 "$SELF/router.py" "$DAIS_HOME" "$proj" 2>/dev/null)"
  [ -z "$agent" ] && continue

  prio="$(pcfg "$proj" priority)"; [[ "$prio" =~ ^[0-9]+$ ]] || prio=100
  lastrun="$(db "SELECT COALESCE(MAX(started_at),'') FROM runs WHERE project='$(sqlesc "$proj")';")"
  eligible+=("$prio|$lastrun|$proj|$agent")
done

# Fill free slots in order: priority ascending (lower = more important, e.g. acme=1),
# then least-recently-run ascending (never-run '' sorts first) so projects rotate fairly.
launched=0
if [ "$free" -gt 0 ] && [ "${#eligible[@]}" -gt 0 ]; then
  while IFS='|' read -r prio lastrun proj agent; do
    [ -z "$proj" ] && continue
    [ "$launched" -ge "$free" ] && break
    if [ "$DRY" = 1 ]; then
      echo "${CY}tick[$proj]: WOULD run $agent  ${CD}(prio $prio, last_run ${lastrun:-never})${C0}"
      launched=$((launched+1)); continue
    fi
    if [ "$MAX" -eq 1 ]; then
      # serial (default): run in the foreground with the full live stream, exactly as before
      echo "${CC}${CB}▸ tick[$proj]: running $agent${C0}"
      "$SELF/run-agent.sh" "$proj" "$agent"
      exit 0   # one agent this tick; the next tick picks up the next thing
    fi
    # parallel: launch in the background (quiet — its stream goes to the log, not the console),
    # stagger by 1s to avoid a git-fetch / db-insert thundering herd. The agent still prints its
    # own one-line start/finish markers, and `dais watch` can stop the whole tree on Ctrl-C.
    echo "${CC}${CB}▸ tick[$proj]: launching $agent  ${CD}(slot $((running+launched+1))/$MAX, prio $prio)${C0}"
    DAIS_QUIET=1 "$SELF/run-agent.sh" "$proj" "$agent" &
    # Reserve the slot NOW, synchronously, with the agent's real pid ($! is run-agent.sh's pid,
    # which is the $$ it writes into this same lock later). run-agent only writes the lock AFTER its
    # slow `git fetch`, so without this the agent is invisible to the next tick's pool count during
    # that window — the dispatcher then sees a phantom-free slot and over-fills the pool (the 4/3).
    echo $! > "$DAIS_HOME/projects/$proj/.lock-$agent"
    disown
    launched=$((launched+1))
    sleep 1
  done < <(printf '%s\n' "${eligible[@]}" | sort -t'|' -k1,1n -k2,2)
fi

# Exit code paces `dais watch`:
#   0  = work in flight (launched something, or agents still running) → drain (short sleep)
#   10 = idle (nothing running, nothing to launch) → poll on the interval
#   20 = backed off (handled earlier)
if [ "$DRY" = 1 ]; then
  [ "$launched" = 0 ] && echo "tick: nothing eligible to run"
  exit 0
fi
if [ "$launched" -gt 0 ] || [ "$running" -gt 0 ]; then
  [ "$launched" = 0 ] && echo "${CD}tick: pool full ($running/$MAX running) — waiting for a slot${C0}"
  exit 0
fi
echo "tick: nothing to run this round"; exit 10
