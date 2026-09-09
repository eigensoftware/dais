#!/usr/bin/env bash
# dispatch.sh [project] [--dry-run]
# One tick of the adaptive scheduler: if there's capacity and nothing already
# running, pick the single most valuable pending action and run it.
# Wire this to a frequent schedule (e.g. every 30 min overnight). The DB state
# decides what runs — not a fixed clock.
set -uo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)"; source "$SELF/lib.sh"

DRY=0; PROJECT=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --*) echo "dispatch: unknown flag: $a" >&2; exit 1 ;;   # a typo'd --dryrun must not run a REAL tick
    *) PROJECT="$a" ;;
  esac
done

# --- one tick at a time (plan 2.1). `dais watch`, a launchd `dais tick`, and a manual tick are
#     independent processes; each counted free slots from its own snapshot and could launch into
#     the same idle project (or overfill the pool). A lock DIRECTORY (mkdir is atomic) under
#     projects/ holds the tick's pid; a second tick that finds a live holder steps aside as idle
#     (exit 10, so `dais watch` waits its interval); a dead holder's lock is reclaimed. Released
#     on exit — the serial path holds it for the whole run, which is exactly right. ---
TICK_LOCK="$DAIS_HOME/projects/.tick.lock"
if [ "$DRY" = 0 ]; then
  mkdir -p "$DAIS_HOME/projects"
  if ! mkdir "$TICK_LOCK" 2>/dev/null; then
    holder="$(cat "$TICK_LOCK/pid" 2>/dev/null)"
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
      echo "${CD}tick: another tick is running (pid $holder) — skipping this one${C0}"; exit 10
    fi
    rm -rf "$TICK_LOCK"
    mkdir "$TICK_LOCK" 2>/dev/null || { echo "tick: could not take the tick lock ($TICK_LOCK)"; exit 10; }
  fi
  echo $$ > "$TICK_LOCK/pid"
  trap 'rm -rf "$TICK_LOCK"' EXIT
fi

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
      [ "$(pcfg "$pj" archived)" = "true" ] && continue   # archived: the dispatcher never touches it
      mp="$(machine_path "$pj")"; [ -n "$mp" ] || continue
      python3 "$SELF/machine.py" recover "$DB" "$mp" "$pj" 2>/dev/null || true
      worktree_prune_sweep "$(repo_path "$pj" 2>/dev/null)"   # reap crashed-run worktrees (safe: live=0)
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

# --- daily loop budget (plan 1.6): dais.yaml `daily_budget:` or `dais watch --budget`
#     (DAIS_DAILY_BUDGET). Over it, the loop launches nothing until tomorrow (UTC) — in every
#     dispatcher, like pause. Per-project budgets are checked in the eligible loop below. ---
fmt_budget(){ # <spent|limit|unit|over> -> "150000/100000 tokens"
  local s l u; IFS='|' read -r s l u _ <<<"$1"
  if [ "$u" = usd ]; then printf '$%.2f/$%.2f' "$s" "$l"; else printf '%s/%s tokens' "$s" "$l"; fi
}
bstate="$(python3 "$SELF/router.py" --daily-budget "$DAIS_HOME" 2>>"$TLOG")"
if [ -n "$bstate" ] && [ "${bstate##*|}" = 1 ]; then
  tlog "daily budget spent: $(fmt_budget "$bstate") — nothing launches until tomorrow"
  echo "${CY}tick: daily budget spent ($(fmt_budget "$bstate")) — the loop idles until tomorrow (raise: dais watch --budget, or dais.yaml daily_budget)${C0}"
  [ "$DRY" = 1 ] && exit 0
  exit 20
fi

# --- machine maintenance: fire each project's system `unblocked` edges whose blockers are all
#     done (machine.py advance) — e.g. blocked → qa_review once the spawned fix lands — so freed
#     work is dispatchable THIS tick instead of stranding in a waiting state. Then the YOLO sweep
#     (design/yolo-mode.md): a project with a live projects/<p>/.yolo marker gets its yolo-tagged
#     human gates auto-fired (confirm-only edges; the engine refuses strong/fact guards), BEFORE
#     eligibility so freed work dispatches this same tick. Marker: `<expiry-epoch|0> [veto-min]`;
#     expired markers are removed here and journaled. ---
if [ "$DRY" = 0 ]; then
  for p in "$DAIS_HOME"/projects/*/; do
    [ -d "$p" ] || continue; pj="$(basename "$p")"
    [ "$(pcfg "$pj" archived)" = "true" ] && continue     # archived: no maintenance either
    mp="$(machine_path "$pj")"; [ -n "$mp" ] || continue
    python3 "$SELF/machine.py" advance "$DB" "$mp" "$pj" 2>/dev/null | while IFS= read -r t; do
      [ -n "$t" ] && echo "${CD}tick[$pj]: unblocked $t${C0}"
    done
    ym="$p/.yolo"
    if [ -f "$ym" ]; then
      y_exp="$(cut -d' ' -f1 "$ym" 2>/dev/null)"; y_veto="$(cut -d' ' -f2 -s "$ym" 2>/dev/null)"
      if [ -n "$y_exp" ] && [ "$y_exp" != 0 ] && [ "$(date +%s)" -gt "$y_exp" ] 2>/dev/null; then
        rm -f "$ym"; tlog "yolo[$pj]: expired — gates restored"
        echo "${CY}tick[$pj]: yolo expired — founder gates restored${C0}"
      else
        python3 "$SELF/machine.py" yolo "$DB" "$mp" "$pj" ${y_veto:+--veto-min "$y_veto"} 2>>"$TLOG" \
          | while IFS= read -r t; do
          [ -n "$t" ] && { tlog "yolo[$pj]: auto-fired $t"; echo "${CY}tick[$pj]: ⚡ yolo auto-fired $t${C0}"; }
        done
      fi
    fi
  done
fi

# --- notifications (plan 4.4): announce what newly waits on the founder — once per arrival.
#     Every REAL tick, before the capacity gates, so a parked loop still tells you. ---
if [ "$DRY" = 0 ]; then
  python3 "$SELF/notify.py" sweep "$DAIS_HOME" 2>>"$TLOG" | while IFS= read -r n; do [ -n "$n" ] && tlog "$n"; done
fi

# --- capacity gates, scoped PER PROVIDER (runs.provider, migration 0007). Two sets, computed
#     once per tick and applied per candidate role in the eligible loop below (a cooled
#     provider's roles are SKIPPED like the no-op throttle skips a role — everything else
#     still dispatches). They were workspace-wide: one Claude window cap parked codex roles
#     whose ChatGPT allotment was untouched, and vice versa.
#     COOLING: providers with a capped run in 90m (the window resets every ~5h) NEWER than
#       that provider's latest success — a success after the cap proves ITS window is back
#       (a codex success says nothing about Claude's), so the loop resumes at once.
#     BACKOFF: providers with 2+ failed runs in 30m newer than their latest success — don't
#       spin on a persistent fault (execution error, outage); a success after it clears it.
#     Pre-0007 rows are NULL and read as anthropic (every historical run was Claude). A db
#     with no provider column at all can't tell providers apart -> 'all' = the old global gate. ---
COOLING=""; BACKOFF=""
if [ -n "$(db "SELECT 1 FROM pragma_table_info('runs') WHERE name='provider';" 2>/dev/null)" ]; then
  COOLING="$(db "SELECT DISTINCT COALESCE(r.provider,'anthropic') FROM runs r WHERE r.status='capped'
                 AND r.started_at > datetime('now','-90 minutes')
                 AND r.started_at > COALESCE((SELECT MAX(s.started_at) FROM runs s WHERE s.status='succeeded'
                       AND COALESCE(s.provider,'anthropic')=COALESCE(r.provider,'anthropic')), '')
                 ORDER BY 1;" | paste -sd, -)"
  BACKOFF="$(db "SELECT p FROM (SELECT COALESCE(r.provider,'anthropic') p, COUNT(*) n FROM runs r
                 WHERE r.status='failed' AND r.started_at > datetime('now','-30 minutes')
                 AND r.started_at > COALESCE((SELECT MAX(s.started_at) FROM runs s WHERE s.status='succeeded'
                       AND COALESCE(s.provider,'anthropic')=COALESCE(r.provider,'anthropic')), '')
                 GROUP BY p) WHERE n >= 2 ORDER BY p;" | paste -sd, -)"
else
  n="$(db "SELECT COUNT(*) FROM runs WHERE status='capped' AND started_at > datetime('now','-90 minutes')
           AND started_at > COALESCE((SELECT MAX(started_at) FROM runs WHERE status='succeeded'), '');")"
  [ "${n:-0}" -gt 0 ] && COOLING="all"
  n="$(db "SELECT COUNT(*) FROM runs WHERE status='failed' AND started_at > datetime('now','-30 minutes')
           AND started_at > COALESCE((SELECT MAX(started_at) FROM runs WHERE status='succeeded'), '');")"
  [ "${n:-0}" -ge 2 ] && BACKOFF="all"
fi
[ -n "$COOLING" ] && tlog "cap cooldown: $COOLING"
[ -n "$BACKOFF" ] && tlog "error backoff: $BACKOFF"
# provider_gate <provider> -> prints why this provider is withheld this tick ('' = free to run)
provider_gate(){
  case ",$COOLING," in *",$1,"*|*",all,"*) echo "cooling — $1 hit its usage cap within 90m"; return;; esac
  case ",$BACKOFF," in *",$1,"*|*",all,"*) echo "backing off — 2+ failed $1 runs in 30m";; esac
}
withheld=0   # candidates a provider gate skipped this tick (paces the exit code below)

# --- parallel width: how many agents may run at once (default 1 = serial, today's behavior).
#     Set by `dais watch <interval> <N>` via DAIS_MAX_PARALLEL; clamped to 1..5. ---
MAX="${DAIS_MAX_PARALLEL:-}"
# no env (a launchd tick, a manual tick): dais.yaml `parallel:` is the workspace default (plan 3.5)
[ -n "$MAX" ] || MAX="$(sed -n 's/^parallel:[[:space:]]*//p' "$DAIS_HOME/dais.yaml" 2>/dev/null | sed 's/[[:space:]]*#.*$//' | head -1)"
[[ "$MAX" =~ ^[0-9]+$ ]] || MAX=1
[ "$MAX" -lt 1 ] && MAX=1
[ "$MAX" -gt 5 ] && MAX=5
[ "$DRY" = 1 ] && echo "${CD}tick: pool width $MAX${C0}"

# how many agents are live right now (across all projects) → how many slots are free this tick
running="$(live_lock_pids | wc -l | tr -d ' ')"
free=$((MAX - running))

# which projects to consider — archived projects never dispatch (hiding a project from the
# board while the watch loop kept spending agents on it would be the worst of both worlds)
projects=()
if [ -n "$PROJECT" ]; then
  if [ "$(pcfg "$PROJECT" archived)" = "true" ]; then
    tlog "tick[$PROJECT]: archived — skipped"
    echo "${CY}tick: $PROJECT is archived — dais unarchive $PROJECT to resume dispatch${C0}"; exit 0
  fi
  projects=("$PROJECT")
else
  for p in "$DAIS_HOME"/projects/*/; do
    [ -d "$p" ] || continue; pj="$(basename "$p")"
    [ "$(pcfg "$pj" archived)" = "true" ] && continue
    projects+=("$pj")
  done
fi

# Reconcile stall markers BEFORE the pool-gated eligible loop below, so it runs every tick
# regardless of free slots — clearing a phantom marker is bookkeeping, not a launch (MAX defaults
# to 1, so `free` is 0 whenever anything is running, and the eligible loop `break`s out before
# touching most projects). The in-loop clear only revisits a marker when the router still
# nominates that role, so a role whose stall-causing task LEFT the board (done/cancelled) is never
# re-checked and its `dais status` STALLED warning lives forever. Sweep every marker: clear any
# whose stored fingerprint no longer matches the role's current dispatch-set (world changed, or
# now empty); a still-valid stall (fp unchanged) is left for the loop's 6h TTL heartbeat.
for proj in "${projects[@]}"; do
  for sm in "$DAIS_HOME/projects/$proj"/.stalled-*; do
    [ -e "$sm" ] || continue
    r="$(basename "$sm")"; r="${r#.stalled-}"
    fp="$(python3 "$SELF/router.py" --dispatch-set "$DAIS_HOME" "$proj" "$r" 2>/dev/null)"
    [ "$fp" = "$(cat "$sm" 2>/dev/null)" ] && continue
    if [ "$DRY" = 1 ]; then echo "${CD}tick[$proj]: WOULD un-stall $r (its stall-world is gone)${C0}"; continue; fi
    rm -f "$sm"; tlog "unstall $proj/$r — stall-world gone (marker cleared)"
  done
done

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
  # per-project daily budget (project.yaml `daily_budget:`): this project sits out today
  pb="$(python3 "$SELF/router.py" --daily-budget "$DAIS_HOME" "$proj" 2>>"$TLOG")"
  if [ -n "$pb" ] && [ "${pb##*|}" = 1 ]; then
    withheld=$((withheld+1))
    tlog "daily budget spent for $proj: $(fmt_budget "$pb") — skipping"
    echo "${CY}tick[$proj]: daily budget spent ($(fmt_budget "$pb")) — skipping until tomorrow${C0}"
    continue
  fi
  livespec="$(live_role_counts "$proj" | paste -sd, -)"   # '' = idle; 'qa=1' = stacking question

  # who runs next is decided by the project's roles config (see harness/router.py) — no role
  # names hardcoded here. The router returns a role to run, or nothing (idle). A role whose LAST
  # run succeeded recently but touched no tasks (run_tasks, the authoritative trail) is THROTTLED
  # — it already said "nothing actionable"; re-dispatching it every tick hot-loops (a lead burned
  # ~12 runs/20min on a proposal it wouldn't submit). Throttling skips the ROLE, not the project:
  # we re-ask the router with that role excluded, so ready work behind a cooled lead still runs.
  #
  # The throttle alone never ESCALATES: a task stuck in a state its role can't resolve (e.g. a
  # superseded design task whose only exit is a founder cancel) wastes one run per 45m forever —
  # death by a thousand cooldowns (a designer burned 9 runs/7h on one orphan). So on the 2nd
  # consecutive no-op run the role is STALLED: projects/<p>/.stalled-<role> stores its dispatch-set
  # fingerprint (router --dispatch-set: the id|status of the tasks it would run for), and the role
  # is skipped until that world CHANGES — the founder cancelling the orphan, or new work arriving,
  # clears it on the next tick with no ceremony. Notes edits don't change the fingerprint, so a
  # role can't un-stall itself by writing notes. A 6h TTL heartbeat keeps cadence roles (which the
  # marker also gates) from going fully dark. `dais status` surfaces the marker.
  agent=""; excl=""
  while :; do
    # stderr goes to the tick journal, not /dev/null: decide-mode reports any internal failure
    # as "idle", and with stderr muzzled too a malformed machine.json idled a project forever
    # with zero diagnostics anywhere. The journal is exactly the "why didn't it launch?" record.
    cand="$(python3 "$SELF/router.py" "$DAIS_HOME" "$proj" "$excl" "$livespec" 2>>"$TLOG")"
    [ -z "$cand" ] && break
    # provider gate (see the capacity gates above): only resolve the role's provider when some
    # provider is actually cooling/backing off — it costs a python startup per candidate.
    if [ -n "$COOLING$BACKOFF" ]; then
      cprov="$(python3 "$SELF/router.py" --agent-config "$DAIS_HOME" "$proj" "$cand" 2>/dev/null | sed -n 's/^provider=//p')"
      gate="$(provider_gate "${cprov:-anthropic}")"
      if [ -n "$gate" ]; then
        withheld=$((withheld+1))
        tlog "$gate; skipping $proj/$cand"
        echo "${CY}tick[$proj]: $gate — skipping $cand${C0}"
        excl="${excl:+$excl,}$cand"
        continue
      fi
    fi
    sm="$DAIS_HOME/projects/$proj/.stalled-$cand"
    if [ -f "$sm" ]; then
      if [ -n "$(find "$sm" -mmin +360 2>/dev/null)" ]; then
        [ "$DRY" = 0 ] && rm -f "$sm"      # TTL heartbeat: allow one probe run; it re-stalls if still fruitless
      else
        fp="$(python3 "$SELF/router.py" --dispatch-set "$DAIS_HOME" "$proj" "$cand" 2>/dev/null)"
        if [ "$fp" = "$(cat "$sm" 2>/dev/null)" ]; then
          tlog "stalled $proj/$cand — world unchanged ($(paste -sd' ' "$sm" 2>/dev/null)); skipping"
          excl="${excl:+$excl,}$cand"
          continue
        fi
        [ "$DRY" = 0 ] && rm -f "$sm"      # the role's world changed — un-stall and dispatch normally
      fi
    fi
    # "Did the last run make progress?" — design/probe-loop-cooldown.md option C: a run is a
    # no-op when the role's dispatch-set reads NOW (after this tick's reconcile) exactly as it
    # did when that run launched (runs.dispatch_fp). A `claim` that a system `interrupt` reverted
    # therefore counts as nothing — the verb-count signal called it progress 14 times in a row.
    # Rows without a fingerprint (pre-0010) keep the verb check: any non-touch verb = progress.
    cur_fp="$(python3 "$SELF/router.py" --dispatch-set "$DAIS_HOME" "$proj" "$cand" 2>/dev/null)"
    noop_expr="CASE WHEN r.dispatch_fp IS NOT NULL THEN (r.dispatch_fp = '$(sqlesc "$cur_fp")')
                    ELSE ((SELECT COUNT(*) FROM run_tasks rt WHERE rt.run_id=r.id AND rt.verb != 'touch') = 0) END"
    if [ -z "$(db "SELECT 1 FROM pragma_table_info('runs') WHERE name='dispatch_fp';" 2>/dev/null)" ]; then
      noop_expr="((SELECT COUNT(*) FROM run_tasks rt WHERE rt.run_id=r.id AND rt.verb != 'touch') = 0)"
    fi
    last="$(db "SELECT r.status || '|' || (r.started_at > datetime('now','-45 minutes')) || '|' || ($noop_expr)
                FROM runs r WHERE r.project='$(sqlesc "$proj")' AND r.agent='$(sqlesc "$cand")'
                ORDER BY r.id DESC LIMIT 1;" 2>/dev/null)"
    if [ "$last" = "succeeded|1|1" ]; then
      streak="$(db "SELECT COUNT(*) FROM (
                      SELECT r.status s, ($noop_expr) n
                      FROM runs r WHERE r.project='$(sqlesc "$proj")'
                        AND r.agent='$(sqlesc "$cand")'
                      ORDER BY r.id DESC LIMIT 2)
                    WHERE s='succeeded' AND n=1;" 2>/dev/null)"
      if [ "${streak:-0}" -ge 2 ] && [ "$DRY" = 0 ]; then
        # A role dispatching into a verify-guarded state (router.py --verify-gated; see
        # machine.role_awaits_verify) must never be PERMANENTLY parked here: that state's own
        # exit edge says resolving it may legitimately take several polling runs (e.g. an async
        # EAS/CI build), each of which can only report progress via a task-set note (verb=
        # 'touch', since no edge fires until the external process finishes) — indistinguishable
        # from a truly stuck task by this streak check alone. The dispatch-set fingerprint
        # (id|status) never changes while such a task correctly waits in the same state across
        # polls, so a STALL marker here would never self-clear — only `dais start` or the 6h TTL
        # would ever run it again. The 45-minute throttle below still paces the polling and
        # self-clears; only the escalation to a standing marker is skipped.
        if [ "$(python3 "$SELF/router.py" --verify-gated "$DAIS_HOME" "$proj" "$cand" 2>/dev/null)" = "1" ]; then
          tlog "no-stall $proj/$cand — dispatch-set awaits a verify guard; polling continues (never permanently parked)"
        else
          fp="$(python3 "$SELF/router.py" --dispatch-set "$DAIS_HOME" "$proj" "$cand" 2>/dev/null)"
          if [ -n "$fp" ]; then    # empty set = cadence-only run, nothing to stall on
            printf '%s\n' "$fp" > "$sm"
            tlog "STALL $proj/$cand — $streak consecutive no-op runs; parked until its tasks change ($(paste -sd' ' "$sm"))"
          fi
        fi
      fi
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
if [ "$withheld" -gt 0 ]; then
  echo "${CY}tick: a provider gate withheld $withheld launch(es) — cooling until its window frees up${C0}"; exit 20
fi
echo "tick: nothing to run this round"; exit 10
