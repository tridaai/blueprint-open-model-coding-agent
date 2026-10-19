-- Shortlist comparisons (round 8): one stored record per
-- comparison — every shortlist profile run once over the same
-- benchmark set through the round-7 benchmark path, under one
-- actor. A profile that cannot serve in this environment is
-- recorded on the comparison as not_servable with its reason,
-- never skipped. The report a reader sees is a fold over the
-- comparison record and the benchmark records it names; the
-- comparison table itself is append-only evidence, like the
-- benchmark table it points at.
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id TEXT PRIMARY KEY,
    set_name      TEXT NOT NULL,
    task_set_hash TEXT NOT NULL,
    actor         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
