-- history states (statechart H): entering a state declared "history": true records where the
-- task came from; an edge targeting "@history" returns it there. First user: deferred/undefer —
-- a proposal parked from `proposed` un-parks back to `proposed`, never skipping the front gate.
ALTER TABLE tasks ADD COLUMN parked_from TEXT;
