#!/usr/bin/env python3
# Formats agent JSONL streams into readable, real-time lines. The provider-specific event
# mapping lives in the provider PACK (harness/providers/<name>/stream.py, plan 5.1):
#   handle(e, emit, acc) -> True when the run must be scored FAILED
#   finish(emit, acc, failed) [optional] -> called once at end of stream (opencode exits without a
#   closing event, so its pack prints ✓ here)
# Both stock packs map onto the SAME markers (💬 🔧 ↳ ✓ ✗ ⚠) so log files and TUI coloring
# stay provider-agnostic. Writes PLAIN text to LOGFILE (so the saved log stays clean) and
# COLOR to the terminal (when stdout is a tty). Bulletproof: any parse problem prints the raw
# line; it never raises — a formatter bug must never fail an agent run.
#   usage: <agent> ... | fmt-stream.py LOGFILE [--provider <pack>]
# The ledger: each pack's acc() feeds ONE normalized usage record, written to <LOG>.usage.json
# at end of stream (run-agent stores it on the run row — migration 0008). No usage event ->
# no sidecar (absent must read as NULL, never zero). Exit 1 when a pack flagged failure.
import sys, os, json, importlib.util

LOG = open(sys.argv[1], "w") if len(sys.argv) > 1 else None
PROVIDER = "anthropic"
if "--provider" in sys.argv:
    try:
        PROVIDER = sys.argv[sys.argv.index("--provider") + 1]
    except IndexError:
        pass
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
C = {"reset":"\033[0m","cyan":"\033[36m","yellow":"\033[33m","dim":"\033[2m",
     "green":"\033[32m","red":"\033[31m"}

def emit(plain, color=None):
    # VS16 (U+FE0F) makes terminals paint a 2-col emoji glyph while advancing 1 col,
    # overdrawing the next chars; strip it so agent text renders as narrow glyphs.
    plain = plain.replace("️", "")
    if LOG:
        try: LOG.write(plain + "\n"); LOG.flush()
        except Exception: pass
    try:
        if COLOR and color: print(C[color] + plain + C["reset"], flush=True)
        else: print(plain, flush=True)
    except Exception: pass

USAGE = None
FAILED = False

def acc(**kw):
    global USAGE
    if USAGE is None:
        USAGE = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                 "output_tokens": 0, "cost_usd": None, "turns": 0, "session_id": None}
    for k, v in kw.items():
        if v is None or k not in USAGE:
            continue
        if k in ("cost_usd", "session_id"):
            USAGE[k] = v
        else:
            USAGE[k] = (USAGE[k] or 0) + int(v or 0)

def _load_pack(name):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers", name, "stream.py")
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("dais_stream_" + name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "handle", None), getattr(mod, "finish", None)
    except Exception:
        return None

HANDLE, FINISH = _load_pack(PROVIDER) or (None, None)
if HANDLE is None:
    emit("  (fmt-stream: no stream mapping for provider '%s' — printing raw lines)" % PROVIDER, "red")

for raw in iter(sys.stdin.readline, ""):
    raw = raw.rstrip("\n")
    if not raw.strip():
        continue
    try:
        e = json.loads(raw)
    except Exception:
        emit("  " + raw, "red" if "error" in raw.lower() else None)
        continue
    if HANDLE is None:
        emit("  " + raw)
        continue
    try:
        if HANDLE(e, emit, acc):
            FAILED = True
    except Exception:
        emit("  " + raw)

if FINISH is not None:
    try:
        FINISH(emit, acc, FAILED)
    except Exception:
        pass

if USAGE is not None and len(sys.argv) > 1:
    try:
        with open(sys.argv[1] + ".usage.json", "w") as fh:
            json.dump(USAGE, fh)
    except Exception:
        pass                                    # the ledger is best-effort; never fail a run
sys.exit(1 if FAILED else 0)
