-- Batch work orders (round 14): one stored record per batch —
-- a named submission of work-order specifications (gold tasks,
-- ad-hoc orders, template instantiations, mixed), its submitter,
-- its batch-level budget ceilings, and its items, each naming
-- the ordinary work order it created and the batch-level state
-- the item stands in (pending, budget_stopped, cancelled). The
-- record is written at submission and afterwards only by
-- recorded acts: a drive pass marking where it stopped, a
-- budget stop, a cancellation, a driver release. Everything a
-- reader sees about a batch's progress — spend, outcomes,
-- who it waits on — is folded on read from the orders' own
-- records; this table stores the submission and its marks,
-- never a progress counter.
CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
