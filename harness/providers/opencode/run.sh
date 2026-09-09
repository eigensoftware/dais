# Provider pack: opencode — `opencode run --format json` (plan 5.5): one adapter for every provider
# opencode knows (75+, incl. local via ollama). Sourced by run-agent.sh (sees MODEL EFF ACCESS
# STANDING PERSONA WORKDIR LOG). No system-prompt flag: the persona is concatenated like codex.
# Access: review/draft roles run opencode's built-in read-only `plan` agent; edit roles the default
# build agent. --dangerously-skip-permissions always: a headless run that hits a permission prompt
# hangs until the max_minutes watchdog kills it (probed 2026-09-09). --variant = reasoning effort.
provider_run(){
  local agent=()
  [ "$ACCESS" = "edit" ] || agent=(--agent plan)
  opencode run --format json --dangerously-skip-permissions --dir "$WORKDIR" \
        ${MODEL:+-m "$MODEL"} \
        ${EFF:+--variant "$EFF"} \
        ${agent[@]+"${agent[@]}"} \
        "$STANDING

$PERSONA" </dev/null 2>&1 \
        | python3 -u "$DAIS_ROOT/harness/fmt-stream.py" "$LOG" --provider opencode
}
