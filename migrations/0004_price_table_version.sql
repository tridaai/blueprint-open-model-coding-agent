-- Cost ledger (round 6): every usage row is stamped with the
-- price-table version in force when the call was recorded. Cost
-- folds price each row under its own version, so a run keeps the
-- prices it was costed under even after the table moves on. Rows
-- recorded before this migration carry NULL and fold as unpriced
-- (price_table_version_unrecorded) — history is never repriced
-- under a table it did not run under.
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS price_table_version INTEGER;
