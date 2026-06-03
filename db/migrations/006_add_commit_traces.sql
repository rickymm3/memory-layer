-- Migration 006: memory_commit_traces
-- Stores one row per memory commit pipeline run (commit/reject/propose/reinforce/refine).
-- This is an append-only audit table — records are never deleted.

CREATE TABLE IF NOT EXISTS memory_commit_traces (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- raw input from extraction
    candidate_text          TEXT        NOT NULL,

    -- final cleaned text (null when rejected)
    final_memory_text       TEXT,

    -- pipeline decision
    decision                TEXT        NOT NULL
        CHECK (decision IN (
            'commit', 'refine_existing', 'supersede_existing',
            'reinforce_existing', 'mark_conflict', 'propose_for_review', 'reject'
        )),

    write_action            TEXT,
    memory_type             TEXT,
    scope                   TEXT,
    confidence              FLOAT,
    lifecycle_action        TEXT,

    -- atom relations (stored as JSON arrays of UUIDs)
    duplicate_atom_ids      JSONB,
    reinforces_atom_ids     JSONB,
    refines_atom_ids        JSONB,
    supersedes_atom_ids     JSONB,
    conflicts_with_atom_ids JSONB,

    -- result pointers
    committed_atom_id       UUID        REFERENCES memory_atoms(id) ON DELETE SET NULL,
    proposal_id             UUID        REFERENCES memory_proposals(id) ON DELETE SET NULL,

    -- critic output
    critic_notes            JSONB,
    rejection_reason        TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commit_traces_decision   ON memory_commit_traces(decision);
CREATE INDEX IF NOT EXISTS idx_commit_traces_created_at ON memory_commit_traces(created_at);
