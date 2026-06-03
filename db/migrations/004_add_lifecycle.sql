-- Migration 004: memory lifecycle / belief revision
--
-- Adds lifecycle tracking to memory_atoms so the system can preserve old
-- beliefs while promoting newer, more accurate ones.
--
-- lifecycle_status values:
--   active       — current, should be retrieved normally (default)
--   superseded   — replaced by a newer atom; preserved for audit but demoted
--   deprecated   — manually marked as no longer relevant; demoted from retrieval
--   archived     — hidden from normal retrieval; still inspectable
--   contested    — administratively flagged as disputed (distinct from
--                  signal-computed disagreement_flag)
--
-- retrieval_priority separates "correctness probability" (confidence) from
-- "how often to surface this memory". Defaults to 1.0 (no change). Lower
-- values suppress retrieval without altering confidence.
--
-- Apply to existing databases:
--   psql $DATABASE_URL -f db/migrations/004_add_lifecycle.sql

ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS lifecycle_status       TEXT        NOT NULL DEFAULT 'active'
        CHECK (lifecycle_status IN ('active', 'superseded', 'deprecated', 'archived', 'contested')),
    ADD COLUMN IF NOT EXISTS superseded_by_atom_id  UUID        REFERENCES memory_atoms(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS lifecycle_reason       TEXT,
    ADD COLUMN IF NOT EXISTS retrieval_priority     FLOAT       NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS lifecycle_updated_at   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_lifecycle_status ON memory_atoms(lifecycle_status);
