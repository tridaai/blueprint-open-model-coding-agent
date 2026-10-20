-- Skills library (round 19): the skill gate's records — the
-- benchmark evaluations a skill version's publication is judged
-- by, and the publication history itself. The skill content
-- lives in governed files (skills/, loaded fail-closed); these
-- tables carry only the gate's evidence and acts. Same tables
-- in both dialects; payloads are TEXT carrying the same JSON.
-- Publication standing is a fold over skill_events — published,
-- then perhaps rolled back — never a status edited in place.
CREATE TABLE IF NOT EXISTS skill_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    actor         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_events (
    seq        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
