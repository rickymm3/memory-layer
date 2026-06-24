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
  embedding vector NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memory_atoms
ADD COLUMN IF NOT EXISTS context_summary text;

ALTER TABLE memory_atoms
ADD COLUMN IF NOT EXISTS topic_tags TEXT[] NOT NULL DEFAULT '{}';

-- Phase 4: signal-aggregation columns.
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS support_weight     FLOAT NOT NULL DEFAULT 0.0;
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS opposition_weight  FLOAT NOT NULL DEFAULT 0.0;
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS disagreement_score FLOAT NOT NULL DEFAULT 0.0;
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS last_recomputed_at TIMESTAMPTZ;

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

-- Phase 3: memory_proposals — pending confirmation-path candidates.
-- For existing databases apply db/migrations/002_add_memory_proposals.sql instead.
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

-- Phase 7: lifecycle columns — belief revision without deletion.
-- lifecycle_status: active | superseded | deprecated | archived | contested
-- retrieval_priority: float 0.0–1.0+, separates surfacing frequency from confidence.
ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS lifecycle_status       TEXT        NOT NULL DEFAULT 'active'
        CHECK (lifecycle_status IN ('active', 'superseded', 'deprecated', 'archived', 'contested')),
    ADD COLUMN IF NOT EXISTS superseded_by_atom_id  UUID        REFERENCES memory_atoms(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS lifecycle_reason       TEXT,
    ADD COLUMN IF NOT EXISTS retrieval_priority     FLOAT       NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS lifecycle_updated_at   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_lifecycle_status ON memory_atoms(lifecycle_status);

-- Access tracking: lazy decay computes effective confidence at read time from these fields.
-- last_accessed_at + access_count enable: decay formula at retrieval, annual purge of dead atoms.
ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS access_count     INTEGER NOT NULL DEFAULT 0;

-- Visibility: user-set access boundary. Scope emerges from source data; visibility is explicit.
-- private = only this user, team = auth group, public = open pool.
ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private'
        CHECK (visibility IN ('private', 'team', 'public'));

-- Source user identity on signals: enables unique_source_count and spam detection.
ALTER TABLE memory_signals
    ADD COLUMN IF NOT EXISTS source_user_id TEXT;

-- Unique source count: how many distinct users have written signals for this atom.
-- Populated by recompute_atom_weights(). Boosts retrieval_priority for multi-user facts.
ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS unique_source_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_last_accessed_at   ON memory_atoms(last_accessed_at);
CREATE INDEX IF NOT EXISTS idx_memory_atoms_visibility          ON memory_atoms(visibility);
CREATE INDEX IF NOT EXISTS idx_memory_atoms_unique_source_count ON memory_atoms(unique_source_count);
CREATE INDEX IF NOT EXISTS idx_memory_signals_source_user_id    ON memory_signals(source_user_id);

-- Users: identity anchor for multi-user attribution.
-- Not an auth system — passwords/sessions belong in the dashboard web layer.
CREATE TABLE IF NOT EXISTS users (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username     TEXT        NOT NULL UNIQUE,
    email        TEXT        UNIQUE,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email    ON users(email);

-- Default local user so source_user_id='local_user' signals can be resolved.
INSERT INTO users (username, display_name, created_at)
VALUES ('local_user', 'Local User', now())
ON CONFLICT (username) DO NOTHING;

-- Phase 7: task_runs — per-task provenance for reflect_task.py runs.
-- For existing databases apply db/migrations/005_add_task_runs.sql instead.
CREATE TABLE IF NOT EXISTS task_runs (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT        NOT NULL,
    task_description TEXT        NOT NULL,
    model_used       TEXT,
    files_changed    TEXT,
    test_results     TEXT,
    outcome          TEXT        NOT NULL CHECK (outcome IN ('success', 'partial', 'failed')),
    notes            TEXT,
    lessons_stored   INTEGER     NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_runs_scope      ON task_runs(scope);
CREATE INDEX IF NOT EXISTS idx_task_runs_outcome    ON task_runs(outcome);
CREATE INDEX IF NOT EXISTS idx_task_runs_created_at ON task_runs(created_at);

ALTER TABLE memory_signals
    ADD COLUMN IF NOT EXISTS task_run_id UUID REFERENCES task_runs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memory_signals_task_run_id ON memory_signals(task_run_id);

-- Phase 8: memory_commit_traces — audit trail for every commit pipeline run.
-- decision: commit | refine_existing | supersede_existing | reinforce_existing
--           | mark_conflict | propose_for_review | reject
CREATE TABLE IF NOT EXISTS memory_commit_traces (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_text          TEXT        NOT NULL,
    final_memory_text       TEXT,
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
    duplicate_atom_ids      JSONB,
    reinforces_atom_ids     JSONB,
    refines_atom_ids        JSONB,
    supersedes_atom_ids     JSONB,
    conflicts_with_atom_ids JSONB,
    committed_atom_id       UUID        REFERENCES memory_atoms(id) ON DELETE SET NULL,
    proposal_id             UUID        REFERENCES memory_proposals(id) ON DELETE SET NULL,
    critic_notes            JSONB,
    rejection_reason        TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commit_traces_decision   ON memory_commit_traces(decision);
CREATE INDEX IF NOT EXISTS idx_commit_traces_created_at ON memory_commit_traces(created_at);

-- Phase 9: runtime_context_traces — evaluation audit for every chat retrieval.
-- Tracks which atoms were used vs ignored and why, context status, and final action.
CREATE TABLE IF NOT EXISTS runtime_context_traces (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    task_summary        TEXT,
    retrieved_atom_ids  JSONB,
    used_atom_ids       JSONB,
    ignored_atom_ids    JSONB,
    context_status      TEXT        NOT NULL
        CHECK (context_status IN (
            'sufficient', 'insufficient', 'stale', 'conflicting',
            'unsupported', 'needs_verification',
            'needs_user_clarification', 'unsafe_or_blocked'
        )),
    confidence          FLOAT,
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

-- Phase 10: runtime_response_traces — response draft evaluation audit.
-- One row per chat turn: records draft vs final answer and the evaluator verdict.
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

-- Phase 12: belief_revision_log — immutable audit trail for belief/opinion changes.
-- Fires on every write event (commit, refine, supersede, reinforce) for REVISABLE_TYPES:
--   opinion, preference, decision, lesson, belief.
-- For existing databases apply db/migrations/012_add_belief_revision_log.sql instead.
CREATE TABLE IF NOT EXISTS belief_revision_log (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    atom_id          UUID        NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    prior_atom_id    UUID        REFERENCES memory_atoms(id) ON DELETE SET NULL,
    prior_content    TEXT,
    new_content      TEXT        NOT NULL,
    prior_confidence FLOAT,
    new_confidence   FLOAT       NOT NULL,
    memory_type      TEXT        NOT NULL,
    scope            TEXT,
    event_type       TEXT        NOT NULL
        CHECK (event_type IN ('commit', 'refine', 'supersede', 'reinforce')),
    revision_reason  TEXT,
    source_key       TEXT        NOT NULL DEFAULT 'local_user',
    revised_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_belief_revision_log_atom_id
    ON belief_revision_log(atom_id);
CREATE INDEX IF NOT EXISTS idx_belief_revision_log_prior_atom_id
    ON belief_revision_log(prior_atom_id);
CREATE INDEX IF NOT EXISTS idx_belief_revision_log_scope
    ON belief_revision_log(scope);
CREATE INDEX IF NOT EXISTS idx_belief_revision_log_memory_type
    ON belief_revision_log(memory_type);
CREATE INDEX IF NOT EXISTS idx_belief_revision_log_event_type
    ON belief_revision_log(event_type);
CREATE INDEX IF NOT EXISTS idx_belief_revision_log_revised_at
    ON belief_revision_log(revised_at);
