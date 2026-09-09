-- Probe-loop cooldown (design/probe-loop-cooldown.md, option C). The no-progress throttle and
-- the stall escalation used to ask "did this run fire a non-touch verb?" — and a `claim` that a
-- system `interrupt` reverted one tick later looked exactly like progress (one task was
-- dispatched 14 times overnight). Progress is now a NET STATUS DIFF: run-agent records the
-- fingerprint of the role's dispatch-set (router --dispatch-set: id|status of the tasks in the
-- states this role dispatches for) at launch, after the previous tick's reconcile; the next
-- tick compares it with the current one, also after reconcile. Equal = no net progress.
-- NULL (pre-migration rows) falls back to the verb check.
ALTER TABLE runs ADD COLUMN dispatch_fp TEXT;
