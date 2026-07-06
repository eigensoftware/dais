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

# --- tick journal: every REAL tick's outcome is appended to projects/.watch.log (rotated),
#     so "why didn't that tick launch anything?" is answerable after the fact — the console
#     scrollback is not the only record. One line per outcome; the console output is unchanged. ---
TLOG="$DAIS_HOME/projects/.watch.log"
tlog(){ [ "$DRY" = 0 ] && echo "[$(date '+%F %T')] $*" >> "$TLOG" 2>/dev/null || true; }
if [ "$DRY" = 0 ] && [ -f "$TLOG" ] && [ "$(wc -l < "$TLOG" 2>/dev/null)" -gt 800 ]; then
  tail -400 "$TLOG" > "$TLOG.tmp" && mv "$TLOG.tmp" "$TLOG"
fi

# --- reconcile orphaned state (self-heal across stops/starts): drop dead lock files, and if
#     nothing is actually running, mark any leftover 'running' row interrupted AND rewind its
#     task via the MACHINE: fire each project's own system `interrupt` edge(s) (machine.py
#     recover), so an interrupted task returns to whatever state its machine says re-dispatches
#     it — no hardcoded statuses, correct for any authored machine. Safe because this only fires
#     when live=0 (no agent holds a lock → nothing is genuinely mid-flight). ---
if [ "$DRY" = 0 ]; then
  reap_stale_locks
  if [ -z "$(live_lock_pids)" ]; then
    db "UPDATE runs SET status='interrupted', ended_at=datetime('now') WHERE status='running';"
    for p in "$DAIS_HOME"/projects/*/; do
      [ -d "$p" ] || continue; pj="$(basename "$p")"
      mp="$(machine_path "$pj")"; [ -n "$mp" ] || continue
      python3 "$SELF/machine.py" recover "$DB" "$mp" "$pj" 2>/dev/null || true
    done
  fi
fi

# --- pause sentinel: founder parked the loop (dais pause / dais top). Idle, don't launch.
#     One check here makes pause honored by every dispatcher: watch, a hand-run tick, and the
#     launchd schedule. ---
if [ "$DRY" = 0 ] && [ -f "$DAIS_HOME/projects/.paused" ]; then
  tlog "paused — idling"
  echo "${CY}tick: paused (projects/.paused) — run 'dais resume' to continue${C0}"; exit 10
fi

# --- machine maintenance: fire each project's system `unblocked` edges whose blockers are all
#     done (machine.py advance) — e.g. blocked → qa_review once the spawned fix lands — so freed
#     work is dispatchable THIS tick instead of stranding in a waiting state. ---
if [ "$DRY" = 0 ]; then
  for p in "$DAIS_HOME"/projects/*/; do
    [ -d "$p" ] || continue; pj="$(basename "$p")"
    mp="$(machine_path "$pj")"; [ -n "$mp" ] || continue
    python3 "$SELF/machine.py" advance "$DB" "$mp" "$pj" 2>/dev/null | while IFS= read -r t; do
      [ -n "$t" ] && echo "${CD}tick[$pj]: unblocked $t${C0}"
    done
  done
fi

# --- capacity gate: cool down after a recent cap hit (window resets every ~5h) ---
capped_recent="$(db "SELECT COUNT(*) FROM runs WHERE status='capped' AND started_at > datetime('now','-90 minutes');")"
if [ "${capped_recent:-0}" -gt 0 ]; then
  tlog "cap cooldown ($capped_recent capped run(s) in 90m)"
  echo "${CY}tick: hit the subscription cap within 90 min — cooling down until the window frees up${C0}"; exit 20
fi

# --- error backoff: don't spin on a persistent failure (Execution error, transient outage) ---
fail_recent="$(db "SELECT COUNT(*) FROM runs WHERE status='failed' AND started_at > datetime('now','-30 minutes');")"
if [ "${fail_recent:-0}" -ge 2 ]; then
  tlog "error backoff ($fail_recent failed run(s) in 30m)"
  echo "${CY}tick: 2+ failed runs in last 30 min — backing off (check the latest log; will retry later)${C0}"; exit 20
fi

# --- parallel width: how many agents may run at once (default 1 = serial, today's behavior).
#     Set by `dais watch <interval> <N>` via DAIS_MAX_PARALLEL; clamped to 1..5. ---
MAX="${DAIS_MAX_PARALLEL:-1}"
[[ "$MAX" =~ ^[0-9]+$ ]] || MAX=1
[ "$MAX" -lt 1 ] && MAX=1
[ "$MAX" -gt 5 ] && MAX=5

# how many agents are live right now (across all projects) → how many slots are free this tick
running="$(live_lock_pids | wc -l | tr -d ' ')"
free=$((MAX - running))

# which projects to consider
projects=()
if [ -n "$PROJECT" ]; then projects=("$PROJECT")
else for p in "$DAIS_HOME"/projects/*/; do [ -d "$p" ] && projects+=("$(basename "$p")"); done; fi

# Build the eligible set: each project the router wants to run, as one line
# `priority|last_run|project|agent`. At most one NEW launch per project per tick. A busy
# project is normally skipped — EXCEPT role-concurrency stacking: the router may return the
# already-live role again when its frontmatter `concurrency:` has headroom and more
# dispatchable tasks than live runs exist (never a second role into the same repo).
# priority comes from project.yaml (default 100); last_run is the project's most recent run
# start ('' = never run) for least-recently-run fairness.
eligible=()
for proj in "${projects[@]}"; do
  [ "$free" -le 0 ] && break   # pool full — no slot to fill, so don't bother polling the router
  livespec="$(live_role_counts "$proj" | paste -sd, -)"   # '' = idle; 'qa=1' = stacking question

  # who runs next is decided by the project's roles config (see harness/router.py) — no role
  # names hardcoded here. The router returns a role to run, or nothing (idle). A role whose LAST
  # run succeeded recently but touched no tasks (run_tasks, the authoritative trail) is THROTTLED
  # — it already said "nothing actionable"; re-dispatching it every tick hot-loops (a lead burned
  # ~12 runs/20min on a proposal it wouldn't submit). Throttling skips the ROLE, not the project:
  # we re-ask the router with that role excluded, so ready work behind a cooled lead still runs.
  agent=""; excl=""
  while :; do
    cand="$(python3 "$SELF/router.py" "$DAIS_HOME" "$proj" "$excl" "$livespec" 2>/dev/null)"
    [ -z "$cand" ] && break
    last="$(db "SELECT r.status || '|' || (r.started_at > datetime('now','-45 minutes'))
                         || '|' || (SELECT COUNT(*) FROM run_tasks rt
                                       WHERE rt.run_id=r.id AND rt.verb != 'touch')
                FROM runs r WHERE r.project='$(sqlesc "$proj")' AND r.agent='$(sqlesc "$cand")'
                ORDER BY r.id DESC LIMIT 1;" 2>/dev/null)"
    if [ "$last" = "succeeded|1|0" ]; then
      tlog "throttle $proj/$cand — last run was a recent no-op; cooling 45m (trying next role)"
      excl="${excl:+$excl,}$cand"
      continue
    fi
    agent="$cand"; break
  done
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
      # serial (default): run with the full live stream. Backgrounded + waited (not a plain
      # foreground call) so we can pre-write the lock with the agent's REAL pid — closing the
      # window before run-agent's slow git-fetch where a second dispatcher (a launchd tick, a
      # manual `dais tick`) would see the project idle and double-launch into the same repo.
      tlog "launch $proj/$agent (serial)"
      echo "${CC}${CB}▸ tick[$proj]: running $agent${C0}"
      "$SELF/run-agent.sh" "$proj" "$agent" &
      slot="$(free_lock_slot "$proj" "$agent")" || slot="$DAIS_HOME/projects/$proj/.lock-$agent"
      echo $! > "$slot"
      wait $!; rc=$?
      # a nonzero exit here is a CONFIG failure before any run row exists (missing persona,
      # missing repo) — report idle (10), not work-in-flight (0), or `dais watch` hot-spins
      # on its 10s drain sleep forever with nothing for the error-backoff gate to count.
      [ "$rc" -eq 0 ] && exit 0 || exit 10
    fi
    # parallel: launch in the background (quiet — its stream goes to the log, not the console),
    # stagger by 1s to avoid a git-fetch / db-insert thundering herd. The agent still prints its
    # own one-line start/finish markers, and `dais watch` can stop the whole tree on Ctrl-C.
    tlog "launch $proj/$agent (slot $((running+launched+1))/$MAX)"
    echo "${CC}${CB}▸ tick[$proj]: launching $agent  ${CD}(slot $((running+launched+1))/$MAX, prio $prio)${C0}"
    DAIS_QUIET=1 "$SELF/run-agent.sh" "$proj" "$agent" &
    # Reserve the slot NOW, synchronously, with the agent's real pid ($! is run-agent.sh's pid,
    # which is the $$ it writes into this same lock later). run-agent only writes the lock AFTER its
    # slow `git fetch`, so without this the agent is invisible to the next tick's pool count during
    # that window — the dispatcher then sees a phantom-free slot and over-fills the pool (the 4/3).
    # free_lock_slot picks the first non-live slot (slot 1 = the historical bare lock name).
    slot="$(free_lock_slot "$proj" "$agent")" || slot="$DAIS_HOME/projects/$proj/.lock-$agent"
    echo $! > "$slot"
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
