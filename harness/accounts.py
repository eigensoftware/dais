#!/usr/bin/env python3
"""Accounts (plan 5.4; spec docs/superpowers/specs/2026-09-09-accounts-design.md).

An ACCOUNT is {provider, kind, credential}: `kind: subscription` is a CLI login isolated by its
config directory (the pack's config_dir_var: CLAUDE_CONFIG_DIR for claude, CODEX_HOME for codex);
`kind: api` is a metered key named by key_env. A POOL is an ordered set of equivalent accounts
with a rotation policy. Cap state lives on the account: <DAIS_ACCOUNTS_DIR>/<name>.cooldown =
"<epoch> <model>", written when a run on that account caps, cleared by a later success, and
expired by the account's window (default ~5h, the subscription reset).

The file is user-level (credentials never live in a workspace):

    # ~/.dais/accounts.yaml
    accounts:
      max-a:   {provider: anthropic, kind: subscription, config_dir: ~/.dais/accounts/max-a}
      api-1:   {provider: anthropic, kind: api, key_env: ANTHROPIC_API_KEY_1}
    pools:
      max: {members: [max-a, max-b], policy: least-recently-capped}

Every provider pack has an IMPLICIT account of its own name (the ambient login): a workspace
with no accounts file changes nothing. Only the YAML subset above is parsed (two levels, inline
`{k: v}` / `[a, b]` or block form) — the stdlib has no YAML and dais carries no dependencies.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_WINDOW_S = 18000          # ~5h subscription window (run-agent's MARKER_TTL)
POLICIES = ("least-recently-capped", "round-robin", "first-free")
FIELDS = ("provider", "kind", "config_dir", "key_env", "window")


def accounts_file():
    return os.environ.get("DAIS_ACCOUNTS_FILE") or os.path.expanduser("~/.dais/accounts.yaml")


def markers_dir():
    return os.environ.get("DAIS_ACCOUNTS_DIR") or os.path.expanduser("~/.dais/accounts")


# --- the tiny YAML subset -----------------------------------------------------------------------
def _strip(line):
    return re.sub(r"\s+#.*$", "", line.rstrip()) if not line.lstrip().startswith("#") else ""


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _inline_list(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    return [_scalar(v)] if v else []


def _inline_map(v):
    """{k: v, k: [a, b]} -> dict (lists kept as lists)."""
    out = {}
    body = v.strip()[1:-1]
    depth, cur, parts = 0, "", []
    for ch in body:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    for p in parts:
        if ":" not in p:
            continue
        k, val = p.split(":", 1)
        val = val.strip()
        out[k.strip()] = _inline_list(val) if val.startswith("[") else _scalar(val)
    return out


def parse(text):
    """{'accounts': {name: {...}}, 'pools': {name: {...}}} from the YAML subset."""
    top, section, item, listkey = {}, None, None, None
    for raw in text.splitlines():
        line = _strip(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            section = s.rstrip(":").strip() if s.endswith(":") else None
            item, listkey = None, None
            if section:
                top.setdefault(section, {})
            continue
        if section is None:
            continue
        if indent == 2:
            k, _, v = s.partition(":")
            k, v, listkey = k.strip(), v.strip(), None
            top[section][k] = _inline_map(v) if v.startswith("{") else {}
            item = k
            continue
        if item is None:
            continue
        if s.startswith("- "):
            if listkey:
                top[section][item].setdefault(listkey, []).append(_scalar(s[2:]))
            continue
        k, _, v = s.partition(":")
        k, v = k.strip(), v.strip()
        if v == "":
            listkey = k
            top[section][item][k] = []
        elif v.startswith("["):
            top[section][item][k] = _inline_list(v)
        elif v.startswith("{"):
            top[section][item][k] = _inline_map(v)
        else:
            top[section][item][k] = _scalar(v)
    return {"accounts": top.get("accounts", {}), "pools": top.get("pools", {})}


# --- the registry --------------------------------------------------------------------------------
def load(path=None):
    """The accounts registry: the file's accounts and pools, normalized, plus one implicit account
    per provider pack (the ambient login). Missing file = implicit accounts only."""
    import router
    path = path or accounts_file()
    reg = {"accounts": {}, "pools": {}}
    for p in router.provider_packs():
        reg["accounts"][p] = {"provider": p, "kind": "subscription", "config_dir": "", "key_env": "", "window": ""}
    try:
        with open(path) as fh:
            parsed = parse(fh.read())
    except OSError:
        return reg
    for name, a in parsed["accounts"].items():
        acct = {f: str(a.get(f, "") or "") for f in FIELDS}
        acct["kind"] = acct["kind"] or "subscription"
        acct["config_dir"] = os.path.expanduser(acct["config_dir"]) if acct["config_dir"] else ""
        reg["accounts"][name] = acct
    for name, p in parsed["pools"].items():
        members = p.get("members", [])
        if isinstance(members, str):
            members = _inline_list(members)
        reg["pools"][name] = {"members": list(members), "policy": str(p.get("policy") or POLICIES[0])}
    return reg


def resolve(name, reg=None):
    """The account dict (with 'name') for an account name, or None."""
    reg = reg or load()
    a = reg["accounts"].get(name)
    return dict(a, name=name) if a else None


def members(ref, reg=None):
    """The accounts a reference names, in declared order: 'pool:<p>' -> its members, else the one
    account. Unknown -> []."""
    reg = reg or load()
    if ref.startswith("pool:"):
        pool = reg["pools"].get(ref[5:])
        if not pool:
            return []
        return [a for a in (resolve(m, reg) for m in pool["members"]) if a]
    a = resolve(ref, reg)
    return [a] if a else []


def window_seconds(acct):
    w = str(acct.get("window") or "").strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(h|m|s)?$", w)
    if not m:
        return DEFAULT_WINDOW_S
    n, unit = float(m.group(1)), m.group(2) or "h"
    return int(n * {"h": 3600, "m": 60, "s": 1}[unit])


# --- cap markers ----------------------------------------------------------------------------------
def _marker(name):
    return os.path.join(markers_dir(), name + ".cooldown")


def mark_capped(name, model, now=None):
    import time
    os.makedirs(markers_dir(), exist_ok=True)
    with open(_marker(name), "w") as fh:
        fh.write("%d %s\n" % (int(now if now is not None else time.time()), model or ""))


def clear_capped(name):
    try:
        os.unlink(_marker(name))
    except OSError:
        pass


def _read_marker(name):
    try:
        with open(_marker(name)) as fh:
            parts = fh.read().split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except (OSError, ValueError, IndexError):
        return None


def capped_since(name, now=None, reg=None):
    """The epoch the account capped, if its marker is inside the account's window; else None
    (a stale marker is removed)."""
    import time
    m = _read_marker(name)
    if m is None:
        return None
    now = now if now is not None else time.time()
    acct = resolve(name, reg) or {}
    if now - m[0] >= window_seconds(acct):
        clear_capped(name)
        return None
    return m[0]


def capped_model(name):
    m = _read_marker(name)
    return m[1] if m else ""


# --- selection ------------------------------------------------------------------------------------
def order(ref, now=None, runs_today=None, reg=None):
    """The account NAMES to try for a reference, in order. A single account: itself. A pool: the
    members with no live cap first, by policy — least-recently-capped and first-free keep the
    declared order, round-robin puts the fewest runs today first — then the capped members,
    oldest cap first, as probes (a cap answers at once, and a success clears the marker)."""
    import time
    reg = reg or load()
    now = now if now is not None else time.time()
    runs_today = runs_today or {}
    ms = members(ref, reg)
    if not ref.startswith("pool:"):
        return [a["name"] for a in ms]
    policy = reg["pools"].get(ref[5:], {}).get("policy", POLICIES[0])
    caps = {a["name"]: capped_since(a["name"], now=now, reg=reg) for a in ms}
    free = [a["name"] for a in ms if caps[a["name"]] is None]
    capped = [a["name"] for a in sorted(ms, key=lambda a: caps[a["name"]] or 0) if caps[a["name"]] is not None]
    if policy == "round-robin":
        idx = {n: i for i, n in enumerate(free)}
        free = sorted(free, key=lambda n: (runs_today.get(n, 0), idx[n]))
    return free + capped


def attempts(setup, now=None, runs_today=None, reg=None):
    """The tiers for one run: [(tier, account dict)] — 'primary' members first (same provider),
    then the fallback tier's, when the role has a fallback model."""
    reg = reg or load()
    out = [("primary", resolve(n, reg)) for n in order(setup["account"], now, runs_today, reg)]
    # the fallback tier runs the fallback MODEL, so the same account may appear again (a model
    # swap on one credential is the historical fallback); only an identical tier is dropped
    if setup.get("fallback_model") or (setup.get("fallback_account") and setup["fallback_account"] != setup["account"]):
        for n in order(setup["fallback_account"], now, runs_today, reg):
            out.append(("fallback", resolve(n, reg)))
    return [(t, a) for t, a in out if a]


def _age(secs):
    secs = int(secs)
    return "%dh%02dm" % (secs // 3600, secs % 3600 // 60) if secs >= 3600 else "%dm" % (secs // 60)


def list_text(reg=None, now=None):
    """`dais account list`: every account with its provider, kind, credential, and cap state;
    the pools; the implicit accounts."""
    import time
    reg = reg or load()
    now = now if now is not None else time.time()
    import router
    implicit = set(router.provider_packs())
    out = ["accounts (%s)" % accounts_file()]
    named = [n for n in reg["accounts"] if n not in implicit]
    if not named:
        out.append("  (none — every role runs on its provider's ambient login)")
    for n in named:
        a = reg["accounts"][n]
        cred = a["config_dir"] if a["kind"] == "subscription" else ("$" + a["key_env"] if a["key_env"] else "(no key_env)")
        since = capped_since(n, now=now, reg=reg)
        state = ("cooling %s (%s; window %s)" % (_age(now - since), capped_model(n) or "?", _age(window_seconds(a)))
                 if since is not None else "free")
        out.append("  %-10s %-10s %-13s %-40s %s" % (n, a["provider"], a["kind"], cred, state))
    for pn, pool in reg["pools"].items():
        out.append("  pool %s: %s (%s)" % (pn, ", ".join(pool["members"]), pool["policy"]))
    cooling = [n for n in sorted(implicit) if capped_since(n, now=now, reg=reg) is not None]
    out.append("  implicit: %s (each provider's ambient login%s)"
               % (", ".join(sorted(implicit)), "; cooling: " + ", ".join(cooling) if cooling else ""))
    return "\n".join(out)


def login(name, reg=None):
    """`dais account login <name>`: run the pack's login command with the account's config dir
    exported under the pack's config_dir_var. Returns the exit status."""
    import subprocess
    import router
    reg = reg or load()
    a = resolve(name, reg)
    if not a:
        print("dais account: no account '%s' in %s" % (name, accounts_file()), file=sys.stderr); return 1
    if a["kind"] != "subscription":
        print("dais account: '%s' is an api account — set $%s (env, ~/.dais/env, or $DAIS_HOME/.env); "
              "only subscription accounts log in (its key_env is the credential)" % (name, a["key_env"] or "?"),
              file=sys.stderr); return 1
    pack = router.provider_packs().get(a["provider"], {})
    cmd, var = pack.get("login"), pack.get("config_dir_var", "")
    if not cmd:
        print("dais account: the %s pack declares no login command" % a["provider"], file=sys.stderr); return 1
    env = dict(os.environ)
    if a["config_dir"]:
        if not var:
            print("dais account: the %s pack declares no config_dir_var; the login would land in the ambient profile"
                  % a["provider"], file=sys.stderr); return 1
        os.makedirs(a["config_dir"], exist_ok=True)
        env[var] = a["config_dir"]
    print("dais account: logging in '%s' — %s%s" % (name, " ".join(cmd), (" with %s=%s" % (var, a["config_dir"])) if a["config_dir"] else ""))
    try:
        return subprocess.call(cmd, env=env)
    except OSError as ex:
        print("dais account: %s" % ex, file=sys.stderr); return 1


def main(argv):
    sub = argv[0] if argv else ""
    if sub == "list" or sub == "status":
        print(list_text()); return 0
    if sub == "mark" and len(argv) >= 2:
        mark_capped(argv[1], argv[2] if len(argv) > 2 else ""); return 0
    if sub == "clear" and len(argv) >= 2:
        if not resolve(argv[1]):
            print("dais account: no account '%s'" % argv[1], file=sys.stderr); return 1
        clear_capped(argv[1]); print("cleared the cap marker for %s" % argv[1]); return 0
    if sub == "login" and len(argv) >= 2:
        return login(argv[1])
    print("usage: dais account list | login <name> | clear <name>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
