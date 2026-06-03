-- Migration 013: add source_url and source_type provenance columns to memory_atoms.
--
-- source_type mirrors memory_signals.source_type but denormalized on the atom for
-- fast dashboard filtering without joining to signals.
-- source_url is the origin URL for web_research atoms (NULL for local/user atoms).

ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'local',
    ADD COLUMN IF NOT EXISTS source_url  TEXT;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_source_type ON memory_atoms(source_type);
