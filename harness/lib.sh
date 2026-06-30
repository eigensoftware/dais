# shared helpers — sourced by run-agent.sh and dais. Not executable on its own.
DAIS_ROOT="${DAIS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
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
DAIS_HOME="${DAIS_HOME/#\~/$HOME}"          # expand a leading ~
export DAIS_ROOT DAIS_HOME
DB="$DAIS_HOME/dais.db"

# preflight: every dais/run-agent/dispatch path hits the DB — fail clearly if sqlite3 is absent.
command -v sqlite3 >/dev/null 2>&1 || { echo "dais: 'sqlite3' not found — install SQLite and retry" >&2; exit 127; }

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

# read a single-line field from projects/<project>/project.yaml
pcfg(){ grep -E "^$2:" "$DAIS_HOME/projects/$1/project.yaml" 2>/dev/null | head -1 | sed "s/^$2:[[:space:]]*//"; }

# expand a leading ~ to $HOME
expand(){ printf '%s' "${1/#\~/$HOME}"; }

# repo_path <project> — absolute path to a project's working repo.
# absolute -> as-is; ~ -> $HOME; relative -> ${DAIS_AGENT_REPOS:-<parent of DAIS_ROOT>}/<value>
repo_path(){
  local r; r="$(pcfg "$1" repo)"
  case "$r" in
    /*) printf '%s' "$r" ;;
    "~"/*|"~") printf '%s' "$(expand "$r")" ;;
    *)  printf '%s/%s' "${DAIS_AGENT_REPOS:-$(dirname "$DAIS_ROOT")}" "$r" ;;
  esac
}

# migrations_re <project> — grep -E regex matching this project's migration files in a PR diff.
# project.yaml `migrations_glob:` (default */migrations/*.sql) -> regex (* -> .*, . -> \., anchored at end).
migrations_re(){
  local g; g="$(pcfg "$1" migrations_glob)"; g="${g:-*/migrations/*.sql}"
  # glob -> grep -E: escape dots, turn * into .*, anchor the suffix
  local re="${g//./\\.}"; re="${re//\*/.*}"
  printf '%s$' "$re"
}

# need <tool> <install-hint> — fail loud + actionable when a required CLI is missing.
need(){ command -v "$1" >/dev/null 2>&1 || { printf "dais: '%s' not found — %s\n" "$1" "${2:-install it and retry}" >&2; exit 127; }; }

# --- colors (auto-off when stdout isn't a terminal, or NO_COLOR is set) ---
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C0=$'\e[0m'; CB=$'\e[1m'; CD=$'\e[2m'
  CR=$'\e[31m'; CG=$'\e[32m'; CY=$'\e[33m'; CBL=$'\e[34m'; CM=$'\e[35m'; CC=$'\e[36m'; CW=$'\e[97m'
else
  C0=''; CB=''; CD=''; CR=''; CG=''; CY=''; CBL=''; CM=''; CC=''; CW=''
fi

# Did a claude run die on the subscription cap? Match ONLY the genuine CLI cap message
# ("You've hit your session limit …") — NOT an agent merely reviewing rate-limit / usage-cap
# code, which would otherwise false-positive now that logs stream the agent's reasoning.
is_capped(){ grep -qiE "you'?ve (hit|reached) your (usage|session|5-?hour|weekly) limit" "${1:-/dev/null}" 2>/dev/null; }
