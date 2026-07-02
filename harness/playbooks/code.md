Code work happens in the project's repo (your run starts with the working directory set to it).

Open PRs with `gh` — as ready-for-review, NOT draft; mark the PR ready BEFORE you fire the edge
that hands the task to QA, since a draft can't be merged at the founder gate. Record the PR on the
task (`dais task set <id> --pr <url>`) so reviewers and the founder can find it from the board.
Hand work on by firing your role's machine edge — never by setting a status.

Base any new branch on the freshly-fetched origin/main, never on local refs you happen to find. A
just-merged PR is squash-merged and its branch DELETED on the remote, but a stale local branch or
worktree may linger — the dais board is the source of truth for what's merged: if a task is 'done',
its work IS on origin/main, so build on top of main, never stack on that task's old branch.

## Releases (assemble → founder greenlight → ship)

You may be dispatched on a RELEASE task, not a feature. Your job is to make the release provably
safe BEFORE the founder's greenlight, and boring after it.

At `assemble` (release_open → release_review):
- Fire `assemble` to sweep the approved pool under the release, then AUDIT what it swept (the
  release task's links): for EACH encompassed task's PR — still open, unmerged, and applies
  cleanly to CURRENT origin/main? A branch that fell behind main: construct the merged state in a
  throwaway worktree (cherry-pick onto fresh main) and run the full suite there — green CI on the
  stale branch is NOT proof the squash-merge lands green.
- Cross-PR conflicts: PRs touching the same files, or generating the SAME migration number, must
  be sequenced — decide the merge ORDER and who rebases/renumbers after whom.
- WIP safety: list the in-flight work OUTSIDE the release (claimed/doing tasks, open non-release
  PRs). Flag anything this release forces to rebase or breaks (an API/schema/contract change under
  someone's feet).
- Write the verdict INTO the release task's notes before you stop: exact merge order, what you
  verified per PR, the migration plan, WIP impact, and any risk you could NOT verify. The founder
  greenlights from your notes — an unaudited assemble is not an assemble.
- If anything in the pool is NOT safe to ship, say so explicitly and stop — the founder decides
  (greenlight anyway or abort). Never silently ship around a problem.

At `releasing` (after the greenlight):
- Execute your own written plan: merge in the documented order, verify CI as you go, apply the
  migration plan, deploy per the REPO'S runbook (docs/DEPLOY.md or the project CONTEXT; on
  merge==deploy projects the merge itself IS the deploy — there is no second step).
- VERIFY prod afterward: the deployed revision matches what you shipped and the app answers (the
  runbook's checks). Only then fire `shipped` — firing it is your attestation that it is live and
  verified, and it closes every task the release encompasses.
- CLEAN UP the shipped branches: after prod verifies, delete each merged PR's remote branch
  (`git push origin --delete <branch>`; merging with `gh pr merge --delete-branch` does it up
  front). GUARD: never delete a branch an OPEN PR is based on — deleting a base branch
  auto-closes the child PR. Check with `gh pr list --state open --json baseRefName` first.
  Leave local branches/worktrees alone — another agent may be running in them.
- If ANY step fails: do NOT fire `shipped`, do NOT improvise beyond the runbook. Record exactly
  what happened and where it stopped in the notes; the task stays in `releasing` for the founder
  (a failed release spawns the rollback/fix path).
