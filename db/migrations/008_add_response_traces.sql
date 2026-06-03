-- Migration 008: runtime_response_traces
-- One row per chat turn: records the draft answer, the evaluator verdict,
-- any revision applied, and commit candidates for the Memory Commit Pipeline.

CREATE TABLE IF NOT EXISTS runtime_response_traces (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_message        TEXT,
    draft_answer        TEXT,
    final_answer        TEXT,
    verdict             TEXT        NOT NULL
        CHECK (verdict IN (
            'approved', 'needs_caveat', 'needs_revision',
            'needs_verification', 'blocked'
        )),
    action_followed     BOOLEAN,
    overstatement_risk  TEXT
        CHECK (overstatement_risk IN ('none', 'low', 'medium', 'high')),
    issues              JSONB       NOT NULL DEFAULT '[]'::jsonb,
    commit_candidates   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    reasoning           TEXT,
    context_trace_id    UUID        REFERENCES runtime_context_traces(id)
                            ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_response_traces_created_at
    ON runtime_response_traces(created_at);
CREATE INDEX IF NOT EXISTS idx_response_traces_verdict
    ON runtime_response_traces(verdict);
CREATE INDEX IF NOT EXISTS idx_response_traces_context_trace
    ON runtime_response_traces(context_trace_id);
