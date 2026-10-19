-- Model usage records (round 2): one row per model call, live or
-- scripted. run_id is NULL for calls made outside a run (plan
-- drafting). source is 'api' (counts from the provider's usage
-- object, unit 'tokens') or 'scripted' (the offline model's own
-- accounting, unit 'chars') — the label travels with the numbers so
-- scripted accounting is never read as API usage.
CREATE TABLE IF NOT EXISTS model_usage (
    seq               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id            TEXT,
    work_order_id     TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    model             TEXT,
    source            TEXT NOT NULL,
    unit              TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    latency_ms        INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);
