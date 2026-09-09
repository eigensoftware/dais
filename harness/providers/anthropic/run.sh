# Provider pack: anthropic — `claude -p` with stream-json. Sourced by run-agent.sh, which sets
# MODEL EFF EFFORT_FLAG PERM PROFILE CAP_FLAGS RESUME_ID RESUME_PROMPT STANDING PERSONA WORKDIR LOG
# (see run-agent for each one's meaning). Contract: define provider_run; write JSONL to stdout
# through fmt-stream with --provider <pack>; return the CLI's exit status (pipefail).
provider_run(){
  local resume_flag=()
  if [ -n "${RESUME_ID:-}" ]; then
    resume_flag=(--resume "$RESUME_ID")
    echo "  ↻ resuming session $RESUME_ID on ${TASK_ID:-?} (same role, same task, <6h)" | tee -a "$LOG"
  fi
  claude -p "${RESUME_PROMPT:-$STANDING}" \
        ${resume_flag[@]+"${resume_flag[@]}"} \
        --append-system-prompt "$PERSONA" \
        --model "$MODEL" \
        ${EFFORT_FLAG[@]+"${EFFORT_FLAG[@]}"} \
        "${PERM[@]}" \
        ${PROFILE[@]+"${PROFILE[@]}"} \
        ${CAP_FLAGS[@]+"${CAP_FLAGS[@]}"} \
        --add-dir "$WORKDIR" \
        --output-format stream-json --verbose 2>&1 \
        | python3 -u "$DAIS_ROOT/harness/fmt-stream.py" "$LOG" --provider anthropic
}
