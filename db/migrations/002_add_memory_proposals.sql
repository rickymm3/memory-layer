-- Migration 002: add memory_proposals table.
-- Run this on existing databases to apply the Phase 3 schema change.
-- Fresh databases use db/init.sql which already includes this table.

CREATE TABLE IF NOT EXISTS memory_proposals (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status                TEXT NOT NULL DEFAULT 'pending_review',

    -- candidate content captured at proposal time
    content               TEXT NOT NULL,
    context_summary       TEXT,
    memory_type           TEXT NOT NULL,
    scope                 TEXT,
    confidence            FLOAT NOT NULL DEFAULT 0.8,
    importance            FLOAT NOT NULL DEFAULT 0.5,

    -- reconciliation output
    relationship          TEXT NOT NULL,
    reconciliation_reason TEXT,
    matched_memory_ids    JSONB,

    -- CLI-issued approval token (single-use, time-limited)
    approval_token        TEXT,
    token_expires_at      TIMESTAMPTZ,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memory_proposals_status     ON memory_proposals(status);
CREATE INDEX IF NOT EXISTS idx_memory_proposals_created_at ON memory_proposals(created_at);
