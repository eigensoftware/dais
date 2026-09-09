"""anthropic stream: Claude Code's --output-format stream-json -> the shared markers.
Contract: handle(e, emit, acc) -> True when the run must be scored FAILED (a cap stop, an
execution error); emit(text, color) writes a log line; acc(**usage) feeds the ledger."""


def _brief(d, n=160):
    s = str(d).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def handle(e, emit, acc):
    t = e.get("type")
    failed = False
    if t == "result" and isinstance(e.get("usage"), dict):
        u = e["usage"]
        inp = int(u.get("input_tokens") or 0); cw = int(u.get("cache_creation_input_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        acc(input_tokens=inp + cw + cr, cache_read_tokens=cr, cache_write_tokens=cw,
            output_tokens=u.get("output_tokens"), cost_usd=e.get("total_cost_usd"),
            turns=e.get("num_turns"), session_id=e.get("session_id"))
    if t == "assistant":
        for b in e.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                emit("  💬 " + _brief(b["text"], 400), "cyan")
            elif b.get("type") == "tool_use":
                inp = b.get("input", {}) or {}
                # `skill` before description: the lean profile's plugin allowlists are built from
                # WHICH skills a role invokes, and Skill's input carries neither a command nor a path
                hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") \
                    or inp.get("skill") or inp.get("description") or inp.get("path") \
                    or inp.get("prompt") or ""
                emit("  🔧 %s %s" % (b.get("name", "?"), _brief(hint)), "yellow")
    elif t == "user":
        for b in e.get("message", {}).get("content", []):
            if b.get("type") == "tool_result":
                c = b.get("content", "")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                if str(c).strip():
                    emit("     ↳ " + _brief(c, 120), "dim")
    elif t == "result":
        extra = "%ds" % (e["duration_ms"] // 1000) if e.get("duration_ms") else ""
        sub = e.get("subtype", "done")
        if str(sub).startswith("error_"):
            # a cap (--max-turns / --max-budget-usd) or an execution error ended the run before
            # its unit was done: say which, and fail the run (feeds the backoff gate)
            failed = True
            why = {"error_max_turns": "max turns reached (%s)" % e.get("num_turns", "?"),
                   "error_max_budget_usd": "budget cap reached"}.get(sub, sub)
            emit("  ✗ stopped: %s %s" % (why, extra), "red")
        else:
            emit("  ✓ %s %s" % (sub, extra), "green")
    # type == "system" (init noise) intentionally skipped
    return failed
