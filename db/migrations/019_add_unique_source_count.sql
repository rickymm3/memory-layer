-- Migration 019: add unique_source_count to memory_atoms
--
-- Tracks how many distinct users have written signals for each atom.
-- Used in retrieval_priority to boost facts corroborated by multiple
-- independent sources. Populated by recompute_atom_weights().

ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS unique_source_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_unique_source_count
    ON memory_atoms(unique_source_count);
