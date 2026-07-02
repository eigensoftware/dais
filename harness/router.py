#!/usr/bin/env python3
# router.py — config-driven scheduler decision + roles linter.
#   router.py <dais_root> <project>         -> prints the role to run next (nothing = idle)
#   router.py --lint <dais_root> [project]  -> validates roles config(s); exit 1 on any error
#
# Model (status-driven board): a status maps to exactly ONE schedulable role (its `handles`); that
# role REACTIVELY owns the status. Reactive handling runs first, by precedence (verify -> build ->
# plan, generalized from config) — this includes a cadence role like the lead, which reactively owns
# needs_scoping AND still runs on its every:Nh clock for periodic discovery. When no reactive work is
# pending, cadence roles run on their interval; else idle.
import sys, os, sqlite3, re

VALID_ACCESS = {"edit", "review", "draft", "none"}


def parse_roles(path):
    """Return (roles, problems). Each role dict: name, access, trigger, handles[], prec, playbook, line.
    `playbook` is the optional 6th column (the role's working-conventions file; '' if omitted)."""
    roles, problems = [], []
    if not os.path.exists(path):
        return roles, problems
    with open(path) as f:
        for ln, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split()
            if len(p) < 5:
                problems.append((ln, "malformed (need 5 columns: name access trigger handles prec)", line))
                continue
            roles.append({
                "name": p[0], "access": p[1], "trigger": p[2],
                "handles": [] if p[3] == "-" else p[3].split(","),
                "prec": int(p[4]) if p[4].lstrip("-").isdigit() else 999,
                "prec_raw": p[4], "playbook": p[5] if len(p) >= 6 else "", "line": ln,
            })
    return roles, problems


def _machine_for(root, project):
    """The machine a project runs — ALWAYS one (dispatch is unconditionally machine-driven). Primary
    signal: the project's own machine.json (seeded from a workflow template). A `machine:` selector
    in project.yaml overrides which built-in; absent both, the `coding` default. No legacy gate."""
    import machine as MC
    ref = None
    projyaml = os.path.join(root, "projects", project, "project.yaml")
    if os.path.exists(projyaml):
        with open(projyaml) as fh:
            mm = re.search(r"(?m)^machine:[ \t]*(\S+)", fh.read())
        ref = mm.group(1) if mm else None
    return MC.project_machine_path(root, project, ref)


def decide(root, project, excluded=None):
    """Which role should run next for this project (or None = idle). `excluded` roles (the
    dispatcher's no-progress throttle) are skipped in BOTH reactive dispatch and cadence, but
    other roles' work still surfaces — a cooled lead must not starve the engineer behind it."""
    excluded = excluded or set()
    roles, _ = parse_roles(os.path.join(root, "projects", project, "roles"))
    if not roles:
        return None
    db = sqlite3.connect(os.path.join(root, "dais.db"))
    db.row_factory = sqlite3.Row

    # 1) reactive dispatch — ALWAYS via the project's machine: the dispatch role of the top pending
    #    task (the machine's edges own state->role; next_role skips blocked/parked states and open
    #    dependencies). Cadence roles (2) still run on their clock for periodic discovery.
    #    trigger=none is DORMANCY and outranks the machine: a shelved role (e.g. a parked project's
    #    lead) is never scheduled even when an edge would dispatch it — none means never scheduled.
    import machine as MC
    dormant = {r["name"] for r in roles if r["trigger"] == "none"}
    role = MC.next_role(db, MC.load(_machine_for(root, project)), project,
                        excluded=excluded | dormant)
    if role:
        return role

    # 2) cadence: a role whose interval has elapsed (only when no reactive work)
    for r in sorted([r for r in roles if r["trigger"].startswith("every:")
                     and r["name"] not in excluded],
                    key=lambda r: r["prec"]):
        m = re.match(r"every:(\d+)h$", r["trigger"])
        if not m:
            continue
        hrs = int(m.group(1))
        last = db.execute("SELECT MAX(started_at) FROM runs WHERE project=? AND agent=?",
                          [project, r["name"]]).fetchone()[0]
        if last is None:
            return r["name"]
        cutoff = db.execute("SELECT datetime('now', ?)", ["-%d hours" % hrs]).fetchone()[0]
        if last < cutoff:
            return r["name"]
    return None  # idle


def lint_project(root, project):
    """Return (errors, warnings) for one project's roles file."""
    errors, warnings = [], []
    rolesf = os.path.join(root, "projects", project, "roles")
    if not os.path.exists(rolesf):
        return errors, warnings  # no roles file -> project just never runs; not an error
    roles, problems = parse_roles(rolesf)
    for ln, msg, line in problems:
        warnings.append("L%d: %s -> %r" % (ln, msg, line))

    # project.yaml must exist and carry the keys the harness reads
    projyaml = os.path.join(root, "projects", project, "project.yaml")
    if not os.path.exists(projyaml):
        errors.append("missing project.yaml")
    else:
        with open(projyaml) as fh:
            text = fh.read()
        for key in ("project", "repo", "stage_goal"):
            if not re.search(r"(?m)^%s:" % re.escape(key), text):
                errors.append("project.yaml missing required key '%s:'" % key)
        m = re.search(r"(?m)^playbook:[ \t]*(\S+)", text)   # the project-wide default playbook
        if m and not _playbook_file(root, project, m.group(1)):
            warnings.append("project.yaml playbook '%s' has no file (looked in projects/%s/playbooks/ "
                            "and harness/playbooks/); roles fall back to no conventions"
                            % (m.group(1), project))
    if not os.path.exists(os.path.join(root, "projects", project, "CONTEXT.md")):
        warnings.append("no CONTEXT.md (agents read it first for project memory)")

    # THE invariant: each handled status maps to exactly one SCHEDULABLE role. Any role with `handles`
    # reactively owns those statuses (including a cadence role like the lead, which also runs on its
    # clock). Two owners → only the lowest-prec one ever runs; the other is silently starved → error.
    # (trigger=none roles like the founder are excluded — they're gates, never scheduled.)
    handler_by_status = {}
    for r in roles:
        if r["trigger"] != "none":
            for s in r["handles"]:
                handler_by_status.setdefault(s, []).append(r["name"])
    for s, names in sorted(handler_by_status.items()):
        if len(names) > 1:
            errors.append("status '%s' is handled by multiple roles %s — a stage must have ONE owner; "
                          "only the lowest-precedence role runs, the rest are silently starved" % (s, names))

    # 2) field sanity (warn, don't fail — the router already degrades safely)
    for r in roles:
        if r["access"] not in VALID_ACCESS:
            warnings.append("role '%s': unknown access '%s' (expected %s); treated as read-only"
                            % (r["name"], r["access"], "|".join(sorted(VALID_ACCESS))))
        if not (r["trigger"] in ("reactive", "none") or re.match(r"every:\d+h$", r["trigger"])):
            warnings.append("role '%s': unrecognized trigger '%s' (expected reactive|every:Nh|none); "
                            "never scheduled" % (r["name"], r["trigger"]))
        if not r["prec_raw"].lstrip("-").isdigit():
            warnings.append("role '%s': non-numeric prec '%s'; defaulted to 999"
                            % (r["name"], r["prec_raw"]))
        if r["trigger"] != "none":  # a schedulable role needs a persona to run as
            persona = os.path.join(root, "projects", project, "agents", r["name"] + ".md")
            if not os.path.exists(persona):
                warnings.append("role '%s': no persona file at projects/%s/agents/%s.md"
                                % (r["name"], project, r["name"]))
        if r["playbook"] and not _playbook_file(root, project, r["playbook"]):
            warnings.append("role '%s': playbook '%s' has no file (looked in projects/%s/playbooks/ "
                            "and harness/playbooks/); falls back to no conventions"
                            % (r["name"], r["playbook"], project))

    # 3) every role the MACHINE dispatches needs a persona file — the scheduler will launch it,
    #    and run-agent exits before recording a run when the persona is missing (silent stall).
    #    Machine roles needn't be in the roles file (that's scheduling metadata), but they must
    #    be runnable.
    try:
        import machine as MC
        m = MC.load(_machine_for(root, project))
        dispatchable = {MC.dispatch_role(m, s) for s in m.get("states", {})} - {None}
        for role in sorted(dispatchable):
            if not os.path.exists(os.path.join(root, "projects", project, "agents", role + ".md")):
                errors.append("machine dispatches role '%s' but projects/%s/agents/%s.md is missing "
                              "— its runs will fail before recording anything" % (role, project, role))
    except Exception:
        pass                                   # machine lint reports its own problems
    return errors, warnings


def _playbook_file(root, project, name):
    """Resolve a playbook name to a file ('' if none): explicit path → project override → tool default.
    Shared by lint and (mirrored in) run-agent.sh's resolution."""
    if not name:
        return ""
    for cand in (name,
                 os.path.join(root, "projects", project, "playbooks", name + ".md"),
                 os.path.join(os.path.dirname(__file__), "playbooks", name + ".md")):
        if os.path.isfile(cand):
            return cand
    return ""


WORKSPACE_CONTEXT_MAX_LINES = 120  # injected into EVERY agent run -> must stay tight


def lint(root, project):
    if project:
        projects = [project]
    else:
        pdir = os.path.join(root, "projects")
        projects = sorted(d for d in os.listdir(pdir)
                          if os.path.exists(os.path.join(pdir, d, "roles")))
    any_err = False
    # workspace-level check (once, not per project): the workspace CONTEXT.md is injected into
    # every agent run, so an oversized one bloats every prompt. Warn (don't fail) past the cap.
    ws_context = os.path.join(root, "CONTEXT.md")
    if os.path.exists(ws_context):
        with open(ws_context) as fh:
            n = sum(1 for _ in fh)
        if n > WORKSPACE_CONTEXT_MAX_LINES:
            print("• workspace: warn   workspace CONTEXT.md is %d lines — keep it tight "
                  "(injected into every agent run)" % n)
    for proj in projects:
        errors, warnings = lint_project(root, proj)
        if not errors and not warnings:
            print("✓ %s: roles OK" % proj)
        for e in errors:
            print("✗ %s: ERROR  %s" % (proj, e)); any_err = True
        for w in warnings:
            print("• %s: warn   %s" % (proj, w))
    return 1 if any_err else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--lint":
        root = sys.argv[2]
        project = sys.argv[3] if len(sys.argv) > 3 else ""
        sys.exit(lint(root, project))
    try:
        excl = set(sys.argv[3].split(",")) - {""} if len(sys.argv) > 3 else set()
        name = decide(sys.argv[1], sys.argv[2], excluded=excl)
        if name:
            print(name)
    except Exception:
        pass  # decide-mode must never crash the scheduler -> any error means idle
