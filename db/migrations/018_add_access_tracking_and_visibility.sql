-- Migration 018: access tracking, visibility, and source user identity
--
-- Adds three capabilities:
-- 1. last_accessed_at + access_count on memory_atoms: enables lazy decay at
--    retrieval time and annual purge of never-accessed atoms (DELETE WHERE
--    last_accessed_at < NOW() - INTERVAL '1 year').
-- 2. visibility on memory_atoms: user-set access boundary (private/team/public).
--    Scope emerges from source data; visibility is an explicit access gate.
-- 3. source_user_id on memory_signals: real user identity behind each signal,
--    enabling unique_source_count and spam detection for multi-user deployments.

ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS access_count     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS visibility       TEXT    NOT NULL DEFAULT 'private'
        CHECK (visibility IN ('private', 'team', 'public'));

ALTER TABLE memory_signals
    ADD COLUMN IF NOT EXISTS source_user_id TEXT;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_last_accessed_at ON memory_atoms(last_accessed_at);
CREATE INDEX IF NOT EXISTS idx_memory_atoms_visibility        ON memory_atoms(visibility);
CREATE INDEX IF NOT EXISTS idx_memory_signals_source_user_id  ON memory_signals(source_user_id);
