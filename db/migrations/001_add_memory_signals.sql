-- Migration 001: Add memory_signals table
-- Phase 2 signal-based architecture: immutable evidence records.
-- Apply to an existing database with:
--   psql $DATABASE_URL -f db/migrations/001_add_memory_signals.sql
-- Fresh databases will get this via db/init.sql automatically.

CREATE TABLE IF NOT EXISTS memory_signals (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- relationship to atoms and signal chains
    memory_atom_id        UUID REFERENCES memory_atoms(id) ON DELETE SET NULL,
    parent_signal_id      UUID REFERENCES memory_signals(id) ON DELETE SET NULL,

    -- source attribution (identifies who/what produced the signal, not the memory scope)
    source_key            TEXT NOT NULL DEFAULT 'local_user',
    source_type           TEXT NOT NULL DEFAULT 'local',
    source_id             TEXT,

    -- claim content
    content               TEXT NOT NULL,
    context_summary       TEXT,
    memory_type           TEXT NOT NULL,
    scope                 TEXT,

    -- optional semantic fields for future weighting
    subject               TEXT,
    stance                TEXT,

    -- reconciliation metadata
    relationship          TEXT,
    certainty             FLOAT,
    intensity             FLOAT,
    confidence            FLOAT,
    importance            FLOAT,

    -- extraction context
    raw_input             TEXT,
    reconciliation_reason TEXT,

    -- extensibility
    metadata              JSONB,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- traceability and signal chains
CREATE INDEX IF NOT EXISTS idx_memory_signals_memory_atom_id   ON memory_signals(memory_atom_id);
CREATE INDEX IF NOT EXISTS idx_memory_signals_parent_signal_id ON memory_signals(parent_signal_id);

-- source filtering: same-source repetition decay and spam-resistance logic
CREATE INDEX IF NOT EXISTS idx_memory_signals_source_key       ON memory_signals(source_key);
CREATE INDEX IF NOT EXISTS idx_memory_signals_source_type      ON memory_signals(source_type);

-- aggregation and domain filtering
CREATE INDEX IF NOT EXISTS idx_memory_signals_scope            ON memory_signals(scope);
CREATE INDEX IF NOT EXISTS idx_memory_signals_subject          ON memory_signals(subject);

-- reconciliation type filtering
CREATE INDEX IF NOT EXISTS idx_memory_signals_relationship     ON memory_signals(relationship);

-- recency weighting queries
CREATE INDEX IF NOT EXISTS idx_memory_signals_created_at       ON memory_signals(created_at);
