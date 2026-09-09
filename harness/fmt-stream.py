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

# --- usage ledger. Each provider reports usage in its own event and shape; normalize both into
# ONE record and write it to <LOG>.usage.json at end of stream (run-agent stores it on the run
# row — migration 0008). Fields: input_tokens = the WHOLE prompt (claude's input_tokens EXCLUDES
# its cache figures, codex's INCLUDES its cached part — both normalized to the total);
# cache_read_tokens / cache_write_tokens; output_tokens; cost_usd (claude's total_cost_usd, the
# API-equivalent even on a subscription; None for codex, which reports no dollar figure); turns;
# session_id (claude only). No usage event -> no sidecar: absent must read as NULL, never zero.
USAGE = None

def _acc(**kw):
    global USAGE
    if USAGE is None:
        USAGE = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                 "output_tokens": 0, "cost_usd": None, "turns": 0, "session_id": None}
    for k, v in kw.items():
        if v is None:
            continue
        if k in ("cost_usd", "session_id"):
            USAGE[k] = v
        else:
            USAGE[k] = (USAGE[k] or 0) + int(v or 0)

def handle_anthropic(e):
    t = e.get("type")
    if t == "result" and isinstance(e.get("usage"), dict):
        u = e["usage"]
        inp = int(u.get("input_tokens") or 0); cw = int(u.get("cache_creation_input_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        _acc(input_tokens=inp + cw + cr, cache_read_tokens=cr, cache_write_tokens=cw,
             output_tokens=u.get("output_tokens"), cost_usd=e.get("total_cost_usd"),
             turns=e.get("num_turns"), session_id=e.get("session_id"))
    if t == "assistant":
        for b in e.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                emit("  💬 " + brief(b["text"], 400), "cyan")
            elif b.get("type") == "tool_use":
                inp = b.get("input", {}) or {}
                # `skill` before description: the lean profile's plugin allowlists are built
                # from WHICH skills a role invokes, and Skill's input carries neither a
                # command nor a path
                hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") \
                    or inp.get("skill") or inp.get("description") or inp.get("path") \
                    or inp.get("prompt") or ""
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
        sub = e.get("subtype", "done")
        if str(sub).startswith("error_"):
            # a cap (--max-turns / --max-budget-usd) or an execution error ended the run
            # before its unit was done: say which, and fail the run (feeds the backoff gate)
            global FAILED
            FAILED = True
            why = {"error_max_turns": "max turns reached (%s)" % e.get("num_turns", "?"),
                   "error_max_budget_usd": "budget cap reached"}.get(sub, sub)
            emit("  ✗ stopped: %s %s" % (why, extra), "red")
        else:
            emit("  ✓ %s %s" % (sub, extra), "green")
    # type == "system" (init noise) intentionally skipped

# codex `exec --json` event shape (captured live, see tests/fixtures/codex-exec.jsonl):
#   {"type":"thread.started","thread_id":...}
#   {"type":"turn.started"}
#   {"type":"item.completed","item":{"id":...,"type":"agent_message","text":...}}
#   {"type":"item.completed","item":{"id":...,"type":"command_execution",
#                                    "command":...,"aggregated_output":...,"exit_code":...}}
#   {"type":"turn.completed","usage":{...}}
# note: item's own type key is "type" (not "item_type" as first sketched).
# codex exits 0 even when the turn dies on a top-level {"type":"error"} (e.g. a model the
# ChatGPT account can't use). Without a signal, run-agent scored such runs 'succeeded' with
# no task changes and the no-op throttle parked the role. This formatter is the one seam that
# sees the event: log it loud and exit 1 at end of stream (pipefail carries it to run-agent,
# which marks the run failed). Item-level errors are advisory (the turn continues) — warn only.
FAILED = False

def handle_openai(e):
    global FAILED
    t = e.get("type", "")
    item = e.get("item", {}) or {}
    it = item.get("type") or ""
    if t == "error":
        FAILED = True
        emit("  ✗ error: " + brief(e.get("message", ""), 400), "red")
    elif t == "item.completed" and it == "error":
        emit("  ⚠ " + brief(item.get("message", ""), 400), "yellow")
    elif t == "item.completed" and it == "agent_message":
        emit("  💬 " + brief(item.get("text", ""), 400), "cyan")
    elif t == "item.completed" and it == "command_execution":
        emit("  🔧 shell %s" % brief(item.get("command", "")), "yellow")
        outp = item.get("aggregated_output", "")
        if str(outp).strip():
            emit("     ↳ " + brief(outp, 120), "dim")
    elif t == "item.completed" and it == "reasoning":
        pass                                    # thinking — skip like claude's system noise
    elif t == "turn.completed":
        u = e.get("usage") or {}
        if isinstance(u, dict) and u:
            _acc(input_tokens=u.get("input_tokens"), cache_read_tokens=u.get("cached_input_tokens"),
                 cache_write_tokens=u.get("cache_write_input_tokens"),
                 output_tokens=u.get("output_tokens"), turns=1)
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

if USAGE is not None and len(sys.argv) > 1:
    try:
        with open(sys.argv[1] + ".usage.json", "w") as fh:
            json.dump(USAGE, fh)
    except Exception:
        pass                                    # the ledger is best-effort; never fail a run
sys.exit(1 if FAILED else 0)
