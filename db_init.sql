CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS memory_atoms (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  content text NOT NULL,
  context_summary text,
  memory_type text NOT NULL DEFAULT 'fact',
  scope text,
  confidence numeric(4,3) NOT NULL DEFAULT 0.800,
  importance numeric(4,3) NOT NULL DEFAULT 0.500,
  embedding_model text NOT NULL DEFAULT 'qwen3-embedding:latest',
  embedding vector(4096) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memory_atoms
ADD COLUMN IF NOT EXISTS context_summary text;

-- ANN indexing is intentionally deferred for now.
-- In this environment, pgvector HNSW does not support vector(4096).
-- Prototype milestone uses exact cosine search against vector(4096).

-- Phase 2: memory_signals — immutable evidence records.
-- For existing databases apply db/migrations/001_add_memory_signals.sql instead.
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

CREATE INDEX IF NOT EXISTS idx_memory_signals_memory_atom_id   ON memory_signals(memory_atom_id);
CREATE INDEX IF NOT EXISTS idx_memory_signals_parent_signal_id ON memory_signals(parent_signal_id);
CREATE INDEX IF NOT EXISTS idx_memory_signals_source_key       ON memory_signals(source_key);
CREATE INDEX IF NOT EXISTS idx_memory_signals_source_type      ON memory_signals(source_type);
CREATE INDEX IF NOT EXISTS idx_memory_signals_scope            ON memory_signals(scope);
CREATE INDEX IF NOT EXISTS idx_memory_signals_subject          ON memory_signals(subject);
CREATE INDEX IF NOT EXISTS idx_memory_signals_relationship     ON memory_signals(relationship);
CREATE INDEX IF NOT EXISTS idx_memory_signals_created_at       ON memory_signals(created_at);

