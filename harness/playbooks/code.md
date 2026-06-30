Code work happens in the project's repo (your run starts with the working directory set to it).

Open PRs with `gh` — as ready-for-review, NOT draft; mark the PR ready BEFORE you hand off to QA,
since a draft can't be merged at the founder gate. When a change is QA-approved and has a PR, park it
`ready_to_merge` (the founder git-merges it). For a finished NON-code deliverable with no PR (a doc,
plan, or research pass), use `needs_review` instead.

Base any new branch on the freshly-fetched origin/main, never on local refs you happen to find. A
just-merged PR is squash-merged and its branch DELETED on the remote, but a stale local branch or
worktree may linger — the dais board is the source of truth for what's merged: if a task is 'done',
its work IS on origin/main, so build on top of main, never stack on that task's old branch.
