-- Run challenges (round 13): the challenge pass's records. A
-- challenge is raised by a deterministic rule over a SUCCEEDED
-- run's stored records — the patch, the approved plan, the task
-- text, the pristine snapshot — and is stored append-only in
-- spirit: it is written once when raised, and afterwards only
-- its status and disposition change, by recorded acts (a
-- reviewer's answer, or supersession when a newer run replaces
-- the run it was raised on). The original severity never
-- changes; a downgrade moves current_severity and keeps the
-- original on the record. An answered challenge is never
-- deleted, and a superseded one keeps its disposition history.
CREATE TABLE IF NOT EXISTS run_challenges (
    challenge_id  TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    work_order_id TEXT NOT NULL,
    rule          TEXT NOT NULL,
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
