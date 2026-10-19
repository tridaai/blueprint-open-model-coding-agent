-- Task templates (round 12): the task library. A template is a
-- stored, versioned record — a task-text pattern with named
-- parameters, a scope pattern, and a suggested step skeleton.
-- Versions are never mutated: editing a template writes a new
-- version row, and publishing flips that version's status as a
-- recorded act by an actor other than its author. Instantiation
-- reads a published version and creates an ordinary work order;
-- the order carries the provenance, the template table is never
-- consulted again for that order.
CREATE TABLE IF NOT EXISTS task_templates (
    template_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
