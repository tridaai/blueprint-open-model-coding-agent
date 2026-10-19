-- Run checkpoints (round 5): append-only records of how far a run
-- got — one after each landed edit, one at each completed plan
-- step. A checkpoint names the run, the plan step it reached, the
-- per-file digests of the plan's in-scope files in the run's
-- workspace, the tool calls so far, and the usage totals so far.
-- It is the basis a resume is checked against: if the workspace no
-- longer digests to the last checkpoint, the resume refuses.
CREATE TABLE IF NOT EXISTS run_checkpoints (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id TEXT NOT NULL UNIQUE,
    run_id        TEXT NOT NULL,
    work_order_id TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
