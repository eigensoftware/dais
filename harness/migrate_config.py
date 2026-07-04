#!/usr/bin/env python3
"""migrate_config.py <home> <project> — mechanical one-shot conversion to the 2026-07-04
config layout: roles-file trigger/prec/playbook + project.yaml model_*/effort_* suffix keys
move into agents/<role>.md frontmatter; the roles file's access column moves into
machine.json roles (the new authority); active_agents and the suffix keys are stripped
from project.yaml; the roles file is deleted. Defaults are NOT written (a reactive
trigger / prec 50 / absent playbook stays implicit). Existing frontmatter keys win and
are never clobbered."""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router


def migrate(home, project):
    pdir = os.path.join(home, "projects", project)
    rolesf = os.path.join(pdir, "roles")
    if not os.path.exists(rolesf):
        print("nothing to migrate: no legacy roles file in projects/%s" % project)
        return 0
    rows, problems = router.parse_roles(rolesf)
    for ln, msg, line in problems:
        print("  ! roles:%d %s -> %r (skipped)" % (ln, msg, line))

    projyaml = os.path.join(pdir, "project.yaml")
    if os.path.exists(projyaml):
        with open(projyaml) as f:
            ytext = f.read()
    else:
        ytext = ""

    # 1) frontmatter into each persona (skip founder — implicit actor, never scheduled)
    for r in rows:
        if r["name"] == "founder":
            continue
        persona = os.path.join(pdir, "agents", r["name"] + ".md")
        if os.path.exists(persona):
            with open(persona) as f:
                body = f.read()
        else:
            body = "You are the %s.\n" % r["name"]
        existing = router.frontmatter(persona) if os.path.exists(persona) else {}
        if body.startswith("---\n"):       # strip any leading block, keyed or not
            body = re.sub(r"\A---\n.*?\n---\n", "", body, flags=re.S)
        fm = {}
        mval = router._yaml_line(ytext, "model_" + r["name"])
        eval_ = router._yaml_line(ytext, "effort_" + r["name"])
        if mval:
            fm["model"] = mval
        if eval_:
            fm["effort"] = eval_
        if r["trigger"] and r["trigger"] != "reactive":
            fm["trigger"] = r["trigger"]
        if r["prec_raw"] and r["prec_raw"] not in ("50",):
            fm["prec"] = r["prec_raw"]
        if r["playbook"]:
            fm["playbook"] = r["playbook"]
        fm.update(existing)               # existing frontmatter wins
        if fm:
            block = "---\n" + "".join("%s: %s\n" % kv for kv in fm.items()) + "---\n"
            with open(persona, "w") as f:
                f.write(block + body)
            print("  agents/%s.md: frontmatter {%s}" % (r["name"], ", ".join(sorted(fm))))
        elif not os.path.exists(persona):
            with open(persona, "w") as f:
                f.write(body)

    # 2) access -> machine.json roles (the authority)
    mfile = os.path.join(pdir, "machine.json")
    if os.path.exists(mfile):
        with open(mfile) as f:
            m = json.load(f)
        roles = m.setdefault("roles", {})
        changed = False
        for r in rows:
            if r["name"] == "founder":
                continue
            cur = roles.setdefault(r["name"], {})
            if cur.get("access") != r["access"]:
                cur["access"] = r["access"]
                changed = True
        if changed:
            with open(mfile, "w") as f:
                json.dump(m, f, indent=2)
                f.write("\n")
            print("  machine.json: roles access written (now authoritative)")

    # 3) strip legacy keys from project.yaml
    if ytext:
        kept = [l for l in ytext.splitlines(True)
                if not re.match(r"^(model|effort)_[a-z_]+:", l)
                and not re.match(r"^active_agents:", l)]
        if kept != ytext.splitlines(True):
            with open(projyaml, "w") as f:
                f.writelines(kept)
            print("  project.yaml: legacy per-role keys / active_agents removed")

    # 4) retire the roles file
    os.remove(rolesf)
    print("  roles file deleted — run: dais lint %s" % project)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: migrate_config.py <home> <project>", file=sys.stderr)
        sys.exit(2)
    sys.exit(migrate(sys.argv[1], sys.argv[2]))
