# shared helpers — sourced by run-agent.sh, dispatch.sh and dais. Not executable on its own.
DAIS_ROOT="${DAIS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
# expand a leading ~ to $HOME (defined before the DAIS_HOME resolution below, which uses it)
expand(){ printf '%s' "${1/#\~/$HOME}"; }
# DAIS_HOME = the workspace (dais.yaml + CONTEXT.md + projects/ + dais.db). Resolution order:
#   1) $DAIS_HOME env (explicit override)
#   2) nearest ancestor of cwd containing a workspace marker (dais.yaml or dais.db) — "the
#      workspace you're standing in" (a fresh clone has dais.yaml before its board exists)
#   3) cwd is inside the tool's own source tree (DAIS_ROOT) -> self-contained dev (so a fresh
#      dev clone never touches a config'd workspace)
#   4) ~/.dais/config  home=/path/to/workspace
#   5) default to the tool dir (self-contained)
if [ -z "${DAIS_HOME:-}" ]; then
  __d="$PWD"
  while [ -n "$__d" ] && [ "$__d" != "/" ]; do
    if [ -f "$__d/dais.yaml" ] || [ -f "$__d/dais.db" ]; then DAIS_HOME="$__d"; break; fi
    __d="$(dirname "$__d")"
  done
fi
if [ -z "${DAIS_HOME:-}" ]; then
  # compare PHYSICAL paths: a symlinked tmpdir (macOS /var -> /private/var) makes the logical
  # DAIS_ROOT and the getcwd-derived $PWD differ, so resolve both before the prefix test.
  __rp="$(cd "$DAIS_ROOT" 2>/dev/null && pwd -P)"; __pp="$(pwd -P)"
  case "$__pp/" in "${__rp:-$DAIS_ROOT}"/*) DAIS_HOME="$DAIS_ROOT" ;; esac
fi
if [ -z "${DAIS_HOME:-}" ] && [ -f "$HOME/.dais/config" ]; then
  DAIS_HOME="$(sed -n 's/^home=//p' "$HOME/.dais/config" | head -1)"
fi
DAIS_HOME="${DAIS_HOME:-$DAIS_ROOT}"
DAIS_HOME="$(expand "$DAIS_HOME")"
export DAIS_ROOT DAIS_HOME
DB="$DAIS_HOME/dais.db"

# need <tool> <install-hint> — fail loud + actionable when a required CLI is missing.
need(){ command -v "$1" >/dev/null 2>&1 || { printf "dais: '%s' not found — %s\n" "$1" "${2:-install it and retry}" >&2; exit 127; }; }

# preflight: every dais/run-agent/dispatch path hits the DB — fail clearly if sqlite3 is absent.
need sqlite3 "install SQLite and retry"

# `.timeout` makes concurrent writers wait for the lock (up to 10s) instead of failing with
# "database is locked" — needed once `dais watch` runs agents in parallel (they share dais.db).
db_init(){
  # apply the base schema (idempotent), then any pending migrations in filename order,
  # recording each in schema_version so it runs exactly once.
  sqlite3 -cmd ".timeout 10000" "$DB" < "$DAIS_ROOT/harness/schema.sql"
  local m base
  for m in "$DAIS_ROOT"/harness/migrations/*.sql; do
    [ -e "$m" ] || continue
    base="$(basename "$m")"
    if [ -z "$(sqlite3 "$DB" "SELECT 1 FROM schema_version WHERE filename='$base';" 2>/dev/null)" ]; then
      # run the migration body inside a transaction: -bail stops sqlite3 at the
      # first error (before COMMIT), so a partial migration rolls back atomically
      # and is NOT recorded as applied. Pipe (not a heredoc) so the SQL body is
      # never subject to shell expansion.
      if { echo "BEGIN;"; cat "$m"; echo "COMMIT;"; } | sqlite3 -bail -cmd ".timeout 10000" "$DB"; then
        sqlite3 "$DB" "INSERT INTO schema_version(filename) VALUES('$base');"
      fi
    fi
  done
}
db(){ [ -f "$DB" ] || db_init >/dev/null 2>&1; sqlite3 -cmd ".timeout 10000" "$DB" "$@"; }

# True if the tasks table has the dependency column (added by migration 0001). Lets the CLI degrade
# gracefully on a dais.db that hasn't had `dais migrate` run yet, instead of erroring on blocked_on.
has_blocked_on(){ [ -n "$(db "SELECT 1 FROM pragma_table_info('tasks') WHERE name='blocked_on';" 2>/dev/null)" ]; }
sqlesc(){ printf '%s' "${1:-}" | sed "s/'/''/g"; }

# Record that the currently-active run touched a task (run_tasks table, migration 0002). Called from
# every task mutation in the dais CLI. A no-op unless DAIS_RUN_ID is set — run-agent.sh exports it for
# the duration of an agent run, so an agent's `dais task ...` calls are attributed to its run, while
# founder actions from a plain shell (no DAIS_RUN_ID) are correctly NOT attributed to any run.
#   $1 = task id   $2 = verb (claim | create | touch; default touch)
# Best-effort: the insert is swallowed so a mutation never fails on account of the link — and on a
# dais.db that hasn't had `dais migrate` run yet (no run_tasks table) it simply records nothing.
link_run_task(){
  case "${DAIS_RUN_ID:-}" in ''|*[!0-9]*) return 0;; esac      # no active run (or non-numeric) -> skip
  db "INSERT INTO run_tasks(run_id,task_id,verb) VALUES($DAIS_RUN_ID,'$(sqlesc "$1")','$(sqlesc "${2:-touch}")');" 2>/dev/null || true
}

# read a single-line field from projects/<project>/project.yaml
pcfg(){ grep -E "^$2:" "$DAIS_HOME/projects/$1/project.yaml" 2>/dev/null | head -1 | sed "s/^$2:[[:space:]]*//"; }

# machine_path <project> -> the project's resolved machine file. Unconditional: the project's own
# machine.json wins, else a `machine:` selector in project.yaml, else the `coding` default. Shared
# by the dais CLI, the dispatcher (recover/advance) and run-agent (the machine-native prompt).
machine_path(){
  local local_m="$DAIS_HOME/projects/$1/machine.json"
  [ -f "$local_m" ] && { printf '%s' "$local_m"; return 0; }
  local mach; mach="$(pcfg "$1" machine 2>/dev/null)"
  DAIS_MACH="$mach" python3 -c "import os,sys;sys.path.insert(0,'$DAIS_ROOT/harness');import machine as M;print(M.default_machine_path('$DAIS_ROOT', os.environ.get('DAIS_MACH') or None))" 2>/dev/null
}

# repo_path <project> — absolute path to a project's working repo.
# absolute -> as-is; ~ -> $HOME; relative -> ${DAIS_AGENT_REPOS:-<parent of DAIS_HOME>}/<value>.
# The default base is the WORKSPACE's parent (DAIS_HOME), not the install dir (DAIS_ROOT), so a
# packaged install (e.g. Homebrew, where DAIS_ROOT is the read-only Cellar) still resolves repos
# next to the workspace; set DAIS_AGENT_REPOS to override.
repo_path(){
  local r base; r="$(pcfg "$1" repo)"
  case "$r" in
    /*) printf '%s' "$r" ;;
    "~"/*|"~") printf '%s' "$(expand "$r")" ;;
    *)
      # relative repo: env override > dais.yaml `agent_repos:` (init writes it and its comment
      # promises it works — it must actually be read) > the workspace's parent dir.
      base="${DAIS_AGENT_REPOS:-}"
      [ -z "$base" ] && base="$(expand "$(sed -n 's/^agent_repos:[[:space:]]*//p' "$DAIS_HOME/dais.yaml" 2>/dev/null | sed 's/[[:space:]]*#.*$//' | head -1)")"
      printf '%s/%s' "${base:-$(dirname "$DAIS_HOME")}" "$r" ;;
  esac
}

# worktree_prune_sweep <repo> [max-age-days] — reap crashed-run worktrees. A run whose EXIT trap
# never fired (SIGKILL, power loss) leaves its private .worktrees/run-<id> behind. `worktree prune`
# clears ones whose dir is already gone; then drop STALE (untouched > max-age-days, default 2)
# run-* worktrees that are provably safe — clean tree AND no unpushed commits. Anything with
# uncommitted OR committed-but-unpushed work is LEFT for the founder. The dispatcher calls this only
# when nothing is live, so it never yanks a worktree out from under a running agent.
worktree_prune_sweep(){
  local repo="$1" days="${2:-2}" wtd w dirty unpushed
  [ -n "$repo" ] || return 0
  git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  git -C "$repo" worktree prune 2>/dev/null || true
  wtd="$repo/.worktrees"; [ -d "$wtd" ] || return 0
  for w in "$wtd"/run-*; do
    [ -d "$w" ] || continue
    [ -n "$(find "$w" -maxdepth 0 -mtime +"$days" 2>/dev/null)" ] || continue   # stale only
    dirty="$(git -C "$w" status --porcelain 2>/dev/null)"
    unpushed="$(git -C "$w" log --branches --not --remotes --oneline -1 2>/dev/null)"
    [ -z "$dirty" ] && [ -z "$unpushed" ] && git -C "$repo" worktree remove --force "$w" 2>/dev/null || true
  done
  git -C "$repo" worktree prune 2>/dev/null || true
}

# --- agent locks. Each running agent holds projects/<p>/.lock-<role> containing its pid; a lock
#     whose pid is dead is stale (a crash left it behind). One vocabulary, three verbs: ---

# live_lock_pids [project] — print the pid of every live agent (one per line), workspace-wide
# or scoped to one project. Empty output = nothing running.
live_lock_pids(){
  local lk pid
  for lk in "$DAIS_HOME"/projects/${1:-*}/.lock-*; do
    [ -e "$lk" ] || continue
    pid="$(cat "$lk" 2>/dev/null)"; [ -n "$pid" ] || continue
    kill -0 "$pid" 2>/dev/null && echo "$pid"
  done
}

# reap_stale_locks — delete lock files whose pid is dead, so a crashed agent can't wedge dispatch.
reap_stale_locks(){
  local lk
  for lk in "$DAIS_HOME"/projects/*/.lock-*; do
    [ -e "$lk" ] || continue
    kill -0 "$(cat "$lk" 2>/dev/null)" 2>/dev/null || rm -f "$lk"
  done
}

# Role concurrency (frontmatter `concurrency: N`) slots a role's lock: slot 1 keeps the
# historical bare name `.lock-<role>` (so concurrency:1 is byte-identical to the old singleton),
# slots 2..N are `.lock-<role>.<n>`. The dot separator keeps role names unambiguous.

# live_role_counts <project> — print 'role=N' (one per line) for the project's LIVE locks,
# slot-suffix aware. Empty output = project idle. Feeds the router's stacking decision.
live_role_counts(){
  local lk pid r
  for lk in "$DAIS_HOME"/projects/$1/.lock-*; do
    [ -e "$lk" ] || continue
    pid="$(cat "$lk" 2>/dev/null)"; kill -0 "$pid" 2>/dev/null || continue
    r="$(basename "$lk")"; r="${r#.lock-}"; r="$(printf '%s' "$r" | sed 's/\.[0-9][0-9]*$//')"
    echo "$r"
  done | sort | uniq -c | awk '{print $2"="$1}'
}

# free_lock_slot <project> <role> — path of the first non-live slot file for the role (may be
# a stale file to overwrite). Prints nothing when all 5 possible slots are live.
free_lock_slot(){
  local i f pid
  for i in 1 2 3 4 5; do
    f="$DAIS_HOME/projects/$1/.lock-$2"; [ "$i" -gt 1 ] && f="$f.$i"
    pid="$(cat "$f" 2>/dev/null)"
    if ! kill -0 "$pid" 2>/dev/null; then echo "$f"; return 0; fi
  done
  return 1
}

# kill_tree <pid> — TERM a process and all its descendants (wrapper → claude → children).
# Tree-walk (not process groups) so it needs no job control; the agent's EXIT trap then marks
# its run interrupted and the task stays put, resuming on the next start.
kill_tree(){ local pid="$1" k; for k in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$k"; done; kill -TERM "$pid" 2>/dev/null; }

# --- colors (auto-off when stdout isn't a terminal, or NO_COLOR is set) ---
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C0=$'\e[0m'; CB=$'\e[1m'; CD=$'\e[2m'
  CR=$'\e[31m'; CG=$'\e[32m'; CY=$'\e[33m'; CBL=$'\e[34m'; CM=$'\e[35m'; CC=$'\e[36m'; CW=$'\e[97m'
else
  C0=''; CB=''; CD=''; CR=''; CG=''; CY=''; CBL=''; CM=''; CC=''; CW=''
fi

# Did a run die on the provider's usage limit (subscription window, plan rate limit, or —
# under auth:api — a 429/credits error)? $2 = provider (default anthropic). Match only
# genuine limit MESSAGES, not an agent merely discussing rate limits in its reasoning.
is_capped(){
  local pats="you'?ve (hit|reached) your (usage|session|5-?hour|weekly) limit"
  case "${2:-anthropic}" in
    openai) pats="$pats|rate limit reached|you'?ve hit your usage limit" ;;
  esac
  # "out of usage credits" is the Claude CLI's metered-model exhaustion (e.g. Fable 5 on the
  # subscription: "You're out of usage credits. Run /usage-credits ... or /model to switch");
  # phrased nothing like the subscription-window limit above, so match it explicitly — without
  # this a Fable-dry run is mis-scored (often "success 0s") and the auto-fallback never fires.
  # The 429 arm is anchored to the API's actual error shapes ("API Error: 429", '"status": 429',
  # "429 {"type":"error"...) — a bare `error.*429` matched an agent merely DISCUSSING a 429 in
  # its log, and one false positive parks the whole loop in the 90m cap cooldown.
  pats="$pats|insufficient_quota|credit balance is too low|out of usage credits"
  pats="$pats|api error: *429|\"status\" *: *429|[^0-9]429[^0-9].{0,40}(rate.?limit|too many requests|overloaded)"
  grep -qiE "$pats" "${1:-/dev/null}" 2>/dev/null
}
