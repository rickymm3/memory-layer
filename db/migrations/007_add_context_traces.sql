-- Migration 007: runtime_context_traces
-- One row per chat evaluation: records which retrieved atoms were used,
-- which were ignored and why, the assessed context status, and the final action.
-- Append-only audit table.

CREATE TABLE IF NOT EXISTS runtime_context_traces (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    task_summary        TEXT,

    -- JSONB arrays of UUID strings
    retrieved_atom_ids  JSONB,
    used_atom_ids       JSONB,

    -- [{atom_id: uuid, reason: str}]
    ignored_atom_ids    JSONB,

    context_status      TEXT        NOT NULL
        CHECK (context_status IN (
            'sufficient', 'insufficient', 'stale', 'conflicting',
            'unsupported', 'needs_verification',
            'needs_user_clarification', 'unsafe_or_blocked'
        )),
    confidence          FLOAT,

    -- [{type, atom_id, severity, resolution}]
    issues              JSONB,
    required_actions    JSONB,

    final_action        TEXT        NOT NULL
        CHECK (final_action IN (
            'answer', 'answer_with_caveat',
            'verify_then_answer', 'ask_user', 'refuse'
        )),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_context_traces_created_at    ON runtime_context_traces(created_at);
CREATE INDEX IF NOT EXISTS idx_context_traces_final_action  ON runtime_context_traces(final_action);
CREATE INDEX IF NOT EXISTS idx_context_traces_ctx_status    ON runtime_context_traces(context_status);
