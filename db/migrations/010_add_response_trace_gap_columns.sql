-- Migration 010: Add gap resolution columns to runtime_response_traces
-- Records the outcome of the pre-draft gap resolution phase so each turn
-- has a full audit trail of what gap searches were attempted.

ALTER TABLE runtime_response_traces
    ADD COLUMN IF NOT EXISTS gap_status              TEXT,
    ADD COLUMN IF NOT EXISTS gap_searches            INTEGER,
    ADD COLUMN IF NOT EXISTS gap_clarifying_question TEXT;

COMMENT ON COLUMN runtime_response_traces.gap_status IS
    'GapResolutionState.status: resolved | needs_human | in_progress';
COMMENT ON COLUMN runtime_response_traces.gap_searches IS
    'Number of targeted gap searches performed before drafting';
COMMENT ON COLUMN runtime_response_traces.gap_clarifying_question IS
    'Clarifying question surfaced when gap_status=needs_human';
