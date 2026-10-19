-- Core records for the Open-Model Coding Agent governance shell
-- (SQLite local profile). Same tables as the Postgres migration;
-- payloads are TEXT carrying the same JSON in both dialects.
CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    gold_task_id  TEXT,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    work_order_id TEXT NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (work_order_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL,
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    event      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
