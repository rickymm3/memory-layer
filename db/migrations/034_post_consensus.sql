-- Migration 034: Consensus synthesis columns on social_posts.
--
-- Adds:
--   embedding              — post topic vector (centroid of primary atoms), used for
--                            cross-user atom similarity matching and bucket routing.
--   contributing_atom_ids  — all atom UUIDs (any user) that informed the current consensus.
--   consensus_body         — living synthesis updated on each regen; original body is immutable.
--   consensus_updated_at   — when the consensus was last regenerated.

ALTER TABLE social_posts
    ADD COLUMN IF NOT EXISTS embedding              vector,
    ADD COLUMN IF NOT EXISTS contributing_atom_ids  UUID[]      NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS consensus_body         TEXT,
    ADD COLUMN IF NOT EXISTS consensus_updated_at   TIMESTAMPTZ;
