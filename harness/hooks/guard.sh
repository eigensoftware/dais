#!/usr/bin/env bash
# dais access guard — a Claude Code PreToolUse hook (plan 5.6). run-agent attaches it (via
# --settings) to every NON-edit role: the role's persona says "never commit, merge or push",
# the --disallowedTools list keeps Edit/Write off, and this closes the shell: a review/draft role
# may read and test, but `git push|commit|merge|rebase|reset`, `gh pr merge|create|close`,
# `rm -rf …` are refused with a message the model sees. Exit 2 = block (the hook contract);
# exit 0 = allow. Stdin: {"tool_name": …, "tool_input": {"command": …}}. DAIS_ACCESS is the
# role's resolved access (edit roles never get this hook; the check below is belt-and-braces).
#
# The classifier reads TOKENS, not the raw string (security review 2026-09-09): each pipeline
# segment is tokenized, env prefixes and wrappers (env, sudo, xargs, nohup, timeout, …) are
# peeled, a shell's -c payload and `eval` are classified recursively, an inline runtime payload
# (python3 -c, node -e, …) is scanned, and git/gh/rm are matched on their parsed subcommand
# and flags (so `/usr/bin/git -C x push`, `rm -fr`, `bash -c "git push"` are all caught, while
# `echo 'git push' > notes`, `grep push src/`, `rm -f tmp` pass). It is a guardrail against a
# role drifting past its remit, not a sandbox: a script file (`bash run.sh`) is not opened.
[ "${DAIS_ACCESS:-edit}" = edit ] && exit 0
DAIS_HOOK_INPUT="$(cat)"; export DAIS_HOOK_INPUT        # python's stdin carries the script below
verdict="$(python3 - <<'PY'
import json, os, re, shlex, sys

SPLIT = {";", "&&", "||", "|", "&", "(", ")", "\n", "|&"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
RUNTIMES = {"perl", "ruby", "node", "deno", "bun", "php"}
WRAPPERS = {"env", "sudo", "doas", "nice", "nohup", "time", "command", "exec", "builtin", "xargs",
            "caffeinate", "timeout", "gtimeout", "stdbuf", "chronic"}
GIT_BLOCK = {"push", "commit", "merge", "rebase", "reset", "cherry-pick", "revert", "pull", "clean", "am"}
GIT_OPT_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
GH_PR_BLOCK = {"merge", "create", "close", "edit", "ready", "reopen", "lock"}
LOOSE = re.compile(r"\bgit\s+(-\S+\s+)*(push|commit|merge|rebase|reset|cherry-pick|revert|pull|clean|am)\b"
                   r"|\bgh\s+pr\s+(merge|create|close|edit|ready)\b|\brm\s+(-\w*[rR]|--recursive)\b")


def tokens(cmd):
    try:
        lx = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        return list(lx)
    except ValueError:
        return cmd.split()


def segments(toks):
    seg = []
    for t in toks:
        if t in SPLIT or t.startswith("$("):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t.lstrip("`$").rstrip("`"))
    if seg:
        yield seg


def peel(argv):
    """Drop VAR=val prefixes and wrapper commands so argv[0] is the command that matters."""
    while argv:
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]
        if not argv:
            return argv
        name = os.path.basename(argv[0])
        if name not in WRAPPERS:
            return argv
        rest = argv[1:]
        while rest and rest[0].startswith("-") and rest[0] != "--":
            rest = rest[1:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        if name in ("timeout", "gtimeout") and rest:
            rest = rest[1:]                                   # the duration
        argv = rest
    return argv


def inline_payload(name, argv):
    """The code string a shell or runtime runs inline (-c / -e / --eval), else None."""
    flags = ("-c",) if name in SHELLS else ("-c", "-e", "--eval", "-p", "--print", "-r")
    for i, t in enumerate(argv[1:], 1):
        if t in flags and i + 1 < len(argv):
            return argv[i + 1]
        if t.startswith("-") and len(t) > 2 and t[1] in "ce" and name in SHELLS and t[-1] == "c":
            return argv[i + 1] if i + 1 < len(argv) else None   # bash -xc '...'
    return None


def bad_git(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_OPT_ARG else 1
    if i >= len(args):
        return False
    sub, rest = args[i], args[i + 1:]
    if sub in GIT_BLOCK:
        return True
    if sub == "branch" and any(a in ("-d", "-D", "--delete", "-m", "-M") or (a.startswith("-") and not a.startswith("--") and ("d" in a or "D" in a)) for a in rest):
        return True
    if sub in ("checkout", "switch") and any(a in ("-b", "-B", "-c", "-C", "--orphan") for a in rest):
        return True
    if sub == "tag" and any(a in ("-d", "--delete") for a in rest):
        return True
    if sub == "stash" and any(a in ("drop", "clear") for a in rest):
        return True
    return False


def bad_gh(args):
    if len(args) >= 2 and args[0] == "pr" and args[1] in GH_PR_BLOCK:
        return True
    if args and args[0] == "api":
        m = [args[i + 1] for i, a in enumerate(args) if a in ("-X", "--method") and i + 1 < len(args)]
        return any(x.upper() != "GET" for x in m) or any(a in ("-f", "-F", "--raw-field", "--field", "--input") for a in args)
    if len(args) >= 2 and args[0] in ("repo", "release", "issue") and args[1] in ("delete", "create", "close", "edit"):
        return True
    return False


def bad_rm(args):
    for a in args:
        if a in ("--recursive", "-r", "-R"):
            return True
        if a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a):
            return True
    return False


def classify(cmd, depth=0):
    """Return the offending segment, or None."""
    if depth > 4:
        return None
    for seg in segments(tokens(cmd)):
        argv = peel(seg)
        if not argv:
            continue
        name = os.path.basename(argv[0])
        if name == "eval":
            hit = classify(" ".join(argv[1:]), depth + 1)
            if hit:
                return hit
        elif name in SHELLS or name.startswith("python"):
            payload = inline_payload(name, argv)
            if payload is not None and (classify(payload, depth + 1) or LOOSE.search(payload)):
                return " ".join(argv)
        elif name in RUNTIMES:
            payload = inline_payload(name, argv)
            if payload is not None and LOOSE.search(payload):
                return " ".join(argv)
        elif name == "git" and bad_git(argv[1:]):
            return " ".join(argv)
        elif name == "gh" and bad_gh(argv[1:]):
            return " ".join(argv)
        elif name == "rm" and bad_rm(argv[1:]):
            return " ".join(argv)
    return None


try:
    d = json.loads(os.environ.get("DAIS_HOOK_INPUT", ""))
except Exception:
    d = {}
if d.get("tool_name") != "Bash":
    sys.exit(0)
ti = d.get("tool_input")
cmd = ti.get("command", "") if isinstance(ti, dict) else ""
hit = classify(cmd)
if hit:
    print(hit)
    sys.exit(2)
sys.exit(0)
PY
)"
rc=$?
if [ "$rc" = 2 ]; then
  echo "dais: a '${DAIS_ACCESS}' role may not run: ${verdict} — review/draft roles read and test; only an edit role commits, pushes, merges, or deletes. Report what you found in the task's notes instead." >&2
  exit 2
fi
exit 0
