-- Model promotion gate (round 7): benchmark runs, promotion
-- evaluations, and the promotion history — the records the gate
-- judges by. Same tables in both dialects; payloads are TEXT
-- carrying the same JSON, so a payload read is the same shape on
-- either store. The promotion history is append-only: the current
-- default is the latest event's target, and rollback is a new
-- event, never an edit to an old one.
CREATE TABLE IF NOT EXISTS benchmark_runs (
    benchmark_id  TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL,
    set_name      TEXT NOT NULL,
    task_set_hash TEXT NOT NULL,
    actor         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotion_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL,
    set_name      TEXT NOT NULL,
    task_set_hash TEXT NOT NULL,
    actor         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotion_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    from_profile TEXT,
    to_profile   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
