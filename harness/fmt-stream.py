#!/usr/bin/env python3
# Formats Claude Code's --output-format stream-json into readable, real-time lines.
#   usage: claude ... | fmt-stream.py [LOGFILE]
# Writes PLAIN text to LOGFILE (so the saved log stays clean) and COLOR to the
# terminal (when stdout is a tty). Bulletproof: any parse problem prints the raw
# line; it never raises — a formatter bug must never fail an agent run.
import sys, os, json

LOG = open(sys.argv[1], "w") if len(sys.argv) > 1 else None
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
C = {"reset":"\033[0m","cyan":"\033[36m","yellow":"\033[33m","dim":"\033[2m",
     "green":"\033[32m","red":"\033[31m"}

def emit(plain, color=None):
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
    except Exception:
        emit("  " + raw)
