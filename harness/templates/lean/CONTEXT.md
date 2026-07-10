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

## Founder decisions / gotchas

(durable decisions agents must honor)
