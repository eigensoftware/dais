"""openai stream: codex `exec --json` events -> the shared markers (fixture:
tests/fixtures/codex-exec.jsonl). Contract as in the anthropic pack: handle(e, emit, acc) ->
True when the run must be scored FAILED. codex exits 0 even when the turn dies on a top-level
{"type":"error"} (e.g. a model the ChatGPT account can't use) — that is the failure signal."""


def _brief(d, n=160):
    s = str(d).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def handle(e, emit, acc):
    t = e.get("type", "")
    item = e.get("item", {}) or {}
    it = item.get("type") or ""
    if t == "error":
        emit("  ✗ error: " + _brief(e.get("message", ""), 400), "red")
        return True
    if t == "item.completed" and it == "error":
        emit("  ⚠ " + _brief(item.get("message", ""), 400), "yellow")
    elif t == "item.completed" and it == "agent_message":
        emit("  💬 " + _brief(item.get("text", ""), 400), "cyan")
    elif t == "item.completed" and it == "command_execution":
        emit("  🔧 shell %s" % _brief(item.get("command", "")), "yellow")
        outp = item.get("aggregated_output", "")
        if str(outp).strip():
            emit("     ↳ " + _brief(outp, 120), "dim")
    elif t == "item.completed" and it == "reasoning":
        pass                                    # thinking — skip like claude's system noise
    elif t == "turn.completed":
        u = e.get("usage") or {}
        if isinstance(u, dict) and u:
            acc(input_tokens=u.get("input_tokens"), cache_read_tokens=u.get("cached_input_tokens"),
                cache_write_tokens=u.get("cache_write_input_tokens"),
                output_tokens=u.get("output_tokens"), turns=1)
        emit("  ✓ done", "green")
    elif t in ("thread.started", "turn.started", "item.started"):
        pass
    else:
        emit("  " + _brief(e, 200))
    return False
