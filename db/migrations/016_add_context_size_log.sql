-- Migration 016: context_size_log
-- Tracks per-turn token estimates and atom retrieval efficiency.
-- Used by the /context-stats dashboard to surface "fat" atoms
-- (retrieved frequently but rarely used) and token trends over time.

CREATE TABLE IF NOT EXISTS context_size_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id TEXT,
    turn_number INTEGER NOT NULL DEFAULT 0,
    retrieved_atom_count INTEGER NOT NULL DEFAULT 0,
    used_atom_count INTEGER NOT NULL DEFAULT 0,
    retrieved_atom_tokens INTEGER NOT NULL DEFAULT 0,
    user_tokens INTEGER NOT NULL DEFAULT 0,
    assistant_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_context_size_log_created ON context_size_log(created_at);
