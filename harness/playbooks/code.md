Code work happens in the project's repo (your run starts with the working directory set to it).

Open PRs with `gh` — as ready-for-review, NOT draft; mark the PR ready BEFORE you fire the edge
that hands the task to QA, since a draft can't be merged at the founder gate. Record the PR on the
task (`dais task set <id> --pr <url>`) so reviewers and the founder can find it from the board.
Hand work on by firing your role's machine edge — never by setting a status.

Base any new branch on the freshly-fetched origin/main, never on local refs you happen to find. A
just-merged PR is squash-merged and its branch DELETED on the remote, but a stale local branch or
worktree may linger — the dais board is the source of truth for what's merged: if a task is 'done',
its work IS on origin/main, so build on top of main, never stack on that task's old branch.
