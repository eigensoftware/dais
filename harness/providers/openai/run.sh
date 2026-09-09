# Provider pack: openai — `codex exec --json`. codex wraps runs in its own filesystem sandbox, and
# workspace-write blocks .git/ writes, which breaks an edit role's core job (commit/branch/PR). So
# edit roles run with the sandbox BYPASSED (founder decision 2026-07-04): trust parity with
# anthropic's bypassPermissions, where the protection is the machine's guards + the founder gates.
# Non-edit roles keep the write sandbox (repo + DAIS_HOME so `dais fire` still works) — codex has
# no per-tool disallows like claude's --disallowedTools, so the sandbox is their structural guard.
provider_run(){
  local sandbox_flags
  if [ "$ACCESS" = "edit" ]; then
    sandbox_flags=(--dangerously-bypass-approvals-and-sandbox)
  else
    sandbox_flags=(--sandbox workspace-write
                   -c 'sandbox_workspace_write.writable_roots=["'"$DAIS_HOME"'"]')
  fi
  # --ephemeral: a headless run is not a session to resume — don't pile one into ~/.codex per tick.
  # </dev/null: codex "reads additional input from stdin" until EOF — under an interactive
  # `dais watch` that is the founder's terminal, so a run would sit waiting on a keypress.
  # 5.2: any OpenAI-compatible endpoint via codex's model_providers config (overridden with -c),
  # or a local model (--oss --local-provider ollama). Keys are the role's frontmatter / project.yaml.
  local ep=()
  [ -n "${LOCAL_PROVIDER:-}" ] && ep+=(--oss --local-provider "$LOCAL_PROVIDER")
  if [ -n "${MODEL_PROVIDER:-}" ]; then
    ep+=(-c "model_provider=\"$MODEL_PROVIDER\"")
    [ -n "${BASE_URL:-}" ] && ep+=(-c "model_providers.$MODEL_PROVIDER.name=\"$MODEL_PROVIDER\"" -c "model_providers.$MODEL_PROVIDER.base_url=\"$BASE_URL\"")
    [ -n "${ENV_KEY:-}" ]  && ep+=(-c "model_providers.$MODEL_PROVIDER.env_key=\"$ENV_KEY\"")
  fi
  codex exec --json --ephemeral --skip-git-repo-check --cd "$WORKDIR" \
        ${MODEL:+-m "$MODEL"} \
        ${ep[@]+"${ep[@]}"} \
        ${EFF:+-c model_reasoning_effort="$EFF"} \
        "${sandbox_flags[@]}" \
        "$STANDING

$PERSONA" </dev/null 2>&1 \
        | python3 -u "$DAIS_ROOT/harness/fmt-stream.py" "$LOG" --provider openai
}
