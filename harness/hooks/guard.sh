#!/usr/bin/env bash
# dais access guard — a Claude Code PreToolUse hook (plan 5.6). run-agent attaches it (via
# --settings) to every NON-edit role: the role's persona says "never commit, merge or push",
# the --disallowedTools list keeps Edit/Write off, and this closes the shell: a review/draft role
# may read and test, but `git push|commit|merge|rebase|reset`, `gh pr merge|create|close`,
# `rm -rf …` are refused with a message the model sees. Exit 2 = block (the hook contract);
# exit 0 = allow. Stdin: {"tool_name": …, "tool_input": {"command": …}}. DAIS_ACCESS is the
# role's resolved access (edit roles never get this hook; the check below is belt-and-braces).
[ "${DAIS_ACCESS:-edit}" = edit ] && exit 0
input="$(cat)"
read -r tool cmd < <(printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
t = d.get("tool_name", "")
c = (d.get("tool_input") or {}).get("command", "") if isinstance(d.get("tool_input"), dict) else ""
print(t, c.replace("\n", " "))' 2>/dev/null)
[ "$tool" = "Bash" ] || exit 0
if printf '%s' "$cmd" | grep -qE '(^|[;&|[:space:]])(git[[:space:]]+(push|commit|merge|rebase|reset|cherry-pick|branch[[:space:]]+-[dD]|checkout[[:space:]]+-[bB])|gh[[:space:]]+pr[[:space:]]+(merge|create|close|edit|ready)|rm[[:space:]]+-[a-zA-Z]*[rR][a-zA-Z]*[[:space:]])'; then
  echo "dais: a '${DAIS_ACCESS}' role may not run: $cmd — review/draft roles read and test; only an edit role commits, pushes, merges, or deletes. Report what you found in the task's notes instead." >&2
  exit 2
fi
exit 0
