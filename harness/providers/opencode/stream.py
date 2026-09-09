"""opencode stream: `opencode run --format json` events -> the shared markers (fixture:
tests/fixtures/opencode-run.jsonl, captured 2026-09-09 on opencode 1.15.7). Each line is
{"type": …, "part": {...}}: text (part.text), tool_use (part.tool, part.state.input/output),
step_finish (part.tokens {input, output, reasoning, cache {read, write}}, part.cost), and
error ({"error": {"name", "data": {"message"}}}). opencode exits after the final text without a
closing event (the fixture ends on `text`), so ✓ is printed from finish() at end of stream.
Contract: handle(e, emit, acc) -> failed?; finish(emit, acc, failed)."""

_SEEN = {"steps": 0}


def _brief(d, n=160):
    s = str(d).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def handle(e, emit, acc):
    t = e.get("type", "")
    p = e.get("part") or {}
    if t == "error":
        err = e.get("error") or {}
        msg = (err.get("data") or {}).get("message") if isinstance(err, dict) else None
        emit("  ✗ error: " + _brief(msg or err.get("name") if isinstance(err, dict) else err, 400), "red")
        return True
    if t == "text":
        if str(p.get("text", "")).strip():
            emit("  💬 " + _brief(p.get("text", ""), 400), "cyan")
    elif t == "tool_use":
        st = p.get("state") or {}
        inp = st.get("input") or {}
        hint = inp.get("command") or inp.get("filePath") or inp.get("path") or inp.get("pattern") \
            or inp.get("description") or ""
        emit("  🔧 %s %s" % (p.get("tool", "?"), _brief(hint)), "yellow")
        out = st.get("output", "")
        if str(out).strip():
            emit("     ↳ " + _brief(out, 120), "dim")
    elif t == "step_finish":
        tk = p.get("tokens") or {}
        cache = tk.get("cache") or {}
        cr, cw = int(cache.get("read") or 0), int(cache.get("write") or 0)
        acc(input_tokens=int(tk.get("input") or 0) + cr + cw, cache_read_tokens=cr, cache_write_tokens=cw,
            output_tokens=tk.get("output"), cost_usd=(p.get("cost") if p.get("cost") else None),
            turns=1, session_id=p.get("sessionID"))
        _SEEN["steps"] += 1
    elif t == "step_start":
        _SEEN["steps"] += 1
    else:
        emit("  " + _brief(e, 200))
    return False


def finish(emit, acc, failed):
    if not failed and _SEEN["steps"]:
        emit("  ✓ done", "green")
