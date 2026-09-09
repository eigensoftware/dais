#!/usr/bin/env python3
"""`dais doctor` — the preflight a founder runs before trusting the loop (plan 2.9).

One line per finding: ✓ fine · ⚠ worth knowing · ✗ the loop will fail on this. Exit 1 when
anything is ✗. Reads the workspace the way the harness does (router.cast / agent_setup), so
what it checks is what a run would need: the provider CLIs the cast actually uses, their
logins, API keys for auth:api roles, pending migrations, each project's repo, oversized
CONTEXT files, and the dispatcher's runtime markers.

    doctor.py <DAIS_HOME> <DAIS_ROOT>
"""
import os
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router  # noqa: E402

KEYVAR = {n: m.get("key_var", "") for n, m in router.provider_packs().items()}   # from the packs (5.1)


def _env_files_have(home, var):
    for f in (os.path.join(os.path.expanduser("~"), ".dais", "env"), os.path.join(home, ".env")):
        try:
            with open(f) as fh:
                for line in fh:
                    if line.strip().startswith(var + "="):
                        return True
        except OSError:
            continue
    return False


def _projects(home):
    pdir = os.path.join(home, "projects")
    if not os.path.isdir(pdir):
        return []
    out = []
    for d in sorted(os.listdir(pdir)):
        py = os.path.join(pdir, d, "project.yaml")
        if not os.path.exists(py):
            continue
        with open(py) as fh:
            if router._yaml_line(fh.read(), "archived") == "true":
                continue
        out.append(d)
    return out


def _repo_path(home, project):
    """lib.sh repo_path, in python: absolute / ~ / relative to DAIS_AGENT_REPOS, dais.yaml
    agent_repos:, or the workspace's parent."""
    py = os.path.join(home, "projects", project, "project.yaml")
    with open(py) as fh:
        r = router._yaml_line(fh.read(), "repo")
    if not r:
        return ""
    if r.startswith("/"):
        return r
    if r.startswith("~"):
        return os.path.expanduser(r)
    base = os.environ.get("DAIS_AGENT_REPOS", "")
    if not base:
        dy = os.path.join(home, "dais.yaml")
        if os.path.exists(dy):
            with open(dy) as fh:
                base = os.path.expanduser(router._yaml_line(fh.read(), "agent_repos"))
    return os.path.join(base or os.path.dirname(home), r)


def run(home, root):
    lines, bad = [], False

    def ok(msg):
        lines.append("✓ " + msg)

    def warn(msg):
        lines.append("⚠ " + msg)

    def fail(msg):
        nonlocal bad
        bad = True
        lines.append("✗ " + msg)

    # tools the harness itself needs
    for tool in ("sqlite3", "python3"):
        (ok if shutil.which(tool) else fail)("%s on PATH" % tool + ("" if shutil.which(tool) else " — required"))

    # the cast: which providers, auths, and repos are actually in use
    projects = _projects(home)
    providers, api_roles, full_roles = set(), [], []
    for p in projects:
        for r in router.cast(home, p):
            s = router.agent_setup(home, p, r["name"])
            if s["trigger"] == "none":
                continue
            providers.add(s["provider"])
            if s["auth"] == "api":
                api_roles.append((p, r["name"], s["provider"]))
            if s["provider"] == "anthropic" and s["context"] == "full":
                full_roles.append("%s/%s" % (p, r["name"]))
    for prov in sorted(providers):
        cli = router.PROVIDER_CLI.get(prov)
        if not cli:
            warn("provider %r has no pack under harness/providers/ (packs: %s)" % (prov, ", ".join(router.provider_packs()) or "none"))
            continue
        if shutil.which(cli):
            ok("%s on PATH (roles on provider %s)" % (cli, prov))
            if cli == "codex":
                try:
                    rc = subprocess.run(["codex", "login", "status"], capture_output=True, timeout=20).returncode
                    (ok if rc == 0 else warn)("codex login %s" % ("ok" if rc == 0 else "NOT logged in — run `codex login`"))
                except (OSError, subprocess.TimeoutExpired):
                    warn("codex login status could not be checked")
        else:
            fail("%s not on PATH — every role on provider %s fails at preflight" % (cli, prov))
    if shutil.which("gh"):
        ok("gh on PATH (PRs, dais check)")
    else:
        warn("gh not on PATH — the coding playbook opens PRs with it; `dais check` needs it to resolve a PR's branch")
    for p, role, prov in api_roles:
        var = KEYVAR.get(prov, "")
        if var and (os.environ.get(var) or _env_files_have(home, var)):
            ok("%s/%s auth: api — %s is set" % (p, role, var))
        elif var:
            fail("%s/%s auth: api but %s is not set (env, ~/.dais/env, or %s/.env)" % (p, role, var, home))
    if full_roles:
        warn("context: full on %s — inherits your whole Claude Code install (see README: The agent profile)"
             % ", ".join(full_roles))

    # the board: pending migrations
    db = os.path.join(home, "dais.db")
    if os.path.exists(db):
        try:
            conn = sqlite3.connect(db, timeout=10)
            applied = {r[0] for r in conn.execute("SELECT filename FROM schema_version")}
            mig = sorted(f for f in os.listdir(os.path.join(root, "harness", "migrations")) if f.endswith(".sql"))
            pending = [f for f in mig if f not in applied]
            if pending:
                warn("%d pending migration(s): %s — pause the loop and run `dais migrate`" % (len(pending), ", ".join(pending)))
            else:
                ok("schema up to date (%d migrations)" % len(mig))
        except sqlite3.Error as ex:
            fail("dais.db unreadable: %s" % ex)
    else:
        warn("no dais.db yet (dais init / the first command creates it)")

    # per project: repo, CONTEXT size
    for p in projects:
        repo = _repo_path(home, p)
        if not repo:
            fail("%s: project.yaml has no repo:" % p)
        elif not os.path.isdir(repo):
            fail("%s: repo not found at %s" % (p, repo))
        elif not os.path.isdir(os.path.join(repo, ".git")):
            warn("%s: %s is not a git repo (agents branch, fetch and PR from it)" % (p, repo))
        else:
            try:
                has_origin = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"],
                                            capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                has_origin = False
            (ok if has_origin else warn)("%s: repo %s%s" % (p, repo, "" if has_origin else " has no origin remote"))
        ctx = os.path.join(home, "projects", p, "CONTEXT.md")
        if os.path.exists(ctx) and os.path.getsize(ctx) > 24000:
            warn("%s: CONTEXT.md is %dKB — read by every run; keep it tight" % (p, os.path.getsize(ctx) // 1000))
    ws = os.path.join(home, "CONTEXT.md")
    if not os.path.exists(ws):
        warn("no workspace CONTEXT.md (dais init writes one)")

    # dispatcher runtime markers
    pj = os.path.join(home, "projects")
    pid_f = os.path.join(pj, ".watch.pid")
    if os.path.exists(pid_f):
        try:
            pid = int(open(pid_f).read().split()[0])
            os.kill(pid, 0)
            ok("dais watch running (pid %d)" % pid)
        except (ValueError, OSError, IndexError):
            warn("stale projects/.watch.pid (no live loop) — safe to delete")
    lock = os.path.join(pj, ".tick.lock", "pid")
    if os.path.exists(lock):
        try:
            os.kill(int(open(lock).read().strip()), 0)
            ok("a tick is running")
        except (ValueError, OSError):
            warn("stale projects/.tick.lock — the next tick reclaims it")
    if os.path.exists(os.path.join(pj, ".paused")):
        warn("the loop is PAUSED (dais resume)")
    for p in projects:
        for f in os.listdir(os.path.join(pj, p)):
            if f.startswith(".stalled-"):
                warn("%s: role %s is STALLED (parked until its tasks change)" % (p, f[len(".stalled-"):]))
    return lines, bad


def main(argv):
    home = argv[0] if argv else os.environ.get("DAIS_HOME", ".")
    root = argv[1] if len(argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lines, bad = run(home, root)
    print("dais doctor — %s" % home)
    for l in lines:
        print("  " + l)
    print("  %s" % ("FIX the ✗ lines before running the loop" if bad else "no blockers"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
