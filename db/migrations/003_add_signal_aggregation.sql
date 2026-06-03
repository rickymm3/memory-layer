-- Migration 003: add signal-aggregation columns to memory_atoms.
-- Run this on existing databases to apply the Phase 4 schema change.
-- Fresh databases use db/init.sql which already includes these columns.

ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS support_weight     FLOAT NOT NULL DEFAULT 0.0;
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS opposition_weight  FLOAT NOT NULL DEFAULT 0.0;
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS disagreement_score FLOAT NOT NULL DEFAULT 0.0;
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS last_recomputed_at TIMESTAMPTZ;
