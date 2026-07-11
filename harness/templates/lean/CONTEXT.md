# __PROJECT__ — Project Context

> Every agent reads this at the start of a run. Durable north-star, targets,
> founder decisions, and gotchas live here.

## North star

(Describe what __PROJECT__ is and what "good" looks like.)

## The lean shape

One builder, and you are the whole review gate. You file work straight to
`ready` (no lead, no proposal lane); the engineer builds one task per run and
hands you a PR at `review`; you merge (`dais fire <id> merge --confirm`) or
bounce it (`request_changes`). There is no QA agent — the engineer keeps its
own build/tests green, and your review is the verification. Outgrowing this
(you want proposals triaged, QA before you, releases batched)? Edit
machine.json toward the `coding` template's lanes; the machine is yours.

Want it FULLY autonomous (e.g. a prototype over a weekend)? Tag the merge edge
`"yolo": true` in machine.json, then `dais yolo <project> on --for 48h` — the
review gate auto-fires each tick. Deliberately NOT pre-tagged: on a repo where
merge == deploy, that tag means unattended deploys. Your edit, your call.

## Founder decisions / gotchas

(durable decisions agents must honor)
