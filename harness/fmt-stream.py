#!/usr/bin/env python3
# Formats agent JSONL streams into readable, real-time lines. Two providers:
#   anthropic (default) — Claude Code's --output-format stream-json
#   openai              — codex `exec --json`
#   usage: <agent> ... | fmt-stream.py LOGFILE [--provider openai]
# Both map onto the SAME markers (💬 🔧 ↳ ✓) so log files and TUI coloring stay
# provider-agnostic. Writes PLAIN text to LOGFILE (so the saved log stays clean)
# and COLOR to the terminal (when stdout is a tty). Bulletproof: any parse
# problem prints the raw line; it never raises — a formatter bug must never
# fail an agent run.
import sys, os, json

LOG = open(sys.argv[1], "w") if len(sys.argv) > 1 else None
PROVIDER = "openai" if "openai" in sys.argv[2:] else "anthropic"
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
C = {"reset":"\033[0m","cyan":"\033[36m","yellow":"\033[33m","dim":"\033[2m",
     "green":"\033[32m","red":"\033[31m"}

def emit(plain, color=None):
    # VS16 (U+FE0F) makes terminals paint a 2-col emoji glyph while advancing 1 col,
    # overdrawing the next chars; strip it so agent text renders as narrow glyphs.
    plain = plain.replace("\ufe0f", "")
    if LOG:
        try: LOG.write(plain + "\n"); LOG.flush()
        except Exception: pass
    try:
        if COLOR and color: print(C[color] + plain + C["reset"], flush=True)
        else: print(plain, flush=True)
    except Exception: pass

def brief(d, n=160):
    s = str(d).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")

def handle_anthropic(e):
    t = e.get("type")
    if t == "assistant":
        for b in e.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                emit("  💬 " + brief(b["text"], 400), "cyan")
            elif b.get("type") == "tool_use":
                inp = b.get("input", {}) or {}
                hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") \
                    or inp.get("description") or inp.get("path") or inp.get("prompt") or ""
                emit("  🔧 %s %s" % (b.get("name", "?"), brief(hint)), "yellow")
    elif t == "user":
        for b in e.get("message", {}).get("content", []):
            if b.get("type") == "tool_result":
                c = b.get("content", "")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                if str(c).strip():
                    emit("     ↳ " + brief(c, 120), "dim")
    elif t == "result":
        extra = "%ds" % (e["duration_ms"] // 1000) if e.get("duration_ms") else ""
        emit("  ✓ %s %s" % (e.get("subtype", "done"), extra), "green")
    # type == "system" (init noise) intentionally skipped

# codex `exec --json` event shape (captured live, see tests/fixtures/codex-exec.jsonl):
#   {"type":"thread.started","thread_id":...}
#   {"type":"turn.started"}
#   {"type":"item.completed","item":{"id":...,"type":"agent_message","text":...}}
#   {"type":"item.completed","item":{"id":...,"type":"command_execution",
#                                    "command":...,"aggregated_output":...,"exit_code":...}}
#   {"type":"turn.completed","usage":{...}}
# note: item's own type key is "type" (not "item_type" as first sketched).
def handle_openai(e):
    t = e.get("type", "")
    item = e.get("item", {}) or {}
    it = item.get("type") or ""
    if t == "item.completed" and it == "agent_message":
        emit("  💬 " + brief(item.get("text", ""), 400), "cyan")
    elif t == "item.completed" and it == "command_execution":
        emit("  🔧 shell %s" % brief(item.get("command", "")), "yellow")
        outp = item.get("aggregated_output", "")
        if str(outp).strip():
            emit("     ↳ " + brief(outp, 120), "dim")
    elif t == "item.completed" and it == "reasoning":
        pass                                    # thinking — skip like claude's system noise
    elif t == "turn.completed":
        emit("  ✓ done", "green")
    elif t in ("thread.started", "turn.started", "item.started"):
        pass
    else:
        emit("  " + brief(e, 200))

for raw in iter(sys.stdin.readline, ""):
    raw = raw.rstrip("\n")
    if not raw.strip():
        continue
    try:
        e = json.loads(raw)
    except Exception:
        emit("  " + raw, "red" if "error" in raw.lower() else None)
        continue
    try:
        handle_openai(e) if PROVIDER == "openai" else handle_anthropic(e)
    except Exception:
        emit("  " + raw)
