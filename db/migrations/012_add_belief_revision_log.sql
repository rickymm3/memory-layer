-- Migration 012: belief_revision_log
-- Immutable audit trail for every write event on revisable-type memory atoms.
-- Revisable types: opinion, preference, decision, lesson, belief.
--
-- One row is written per event:
--   commit      — the belief was born (prior_* columns are NULL)
--   refine      — the belief was refined / lightly updated
--   supersede   — the belief was replaced by a conflicting or stronger claim
--   reinforce   — a supporting signal raised the confidence (content unchanged)
--
-- This enables:
--   • "I used to think X, now Y" narrative queries
--   • Confidence trajectory over time (prior_confidence → new_confidence)
--   • Full chain-of-custody from birth through every revision
--
-- Apply to an existing database with:
--   psql $DATABASE_URL -f db/migrations/012_add_belief_revision_log.sql

CREATE TABLE IF NOT EXISTS belief_revision_log (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The atom that exists AFTER this event.
    -- For reinforce: this is the same atom (reinforced in-place).
    -- For refine/supersede: this is the NEW atom.
    atom_id          UUID        NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,

    -- The atom that existed BEFORE (NULL for the initial commit).
    prior_atom_id    UUID        REFERENCES memory_atoms(id) ON DELETE SET NULL,

    -- Full text snapshots.  prior_content is NULL for initial commit.
    -- For reinforce: prior_content == new_content (confidence changed, not text).
    prior_content    TEXT,
    new_content      TEXT        NOT NULL,

    -- Confidence trajectory.  prior_confidence is NULL for initial commit.
    prior_confidence FLOAT,
    new_confidence   FLOAT       NOT NULL,

    -- Belief classification at the time of the event.
    memory_type      TEXT        NOT NULL,
    scope            TEXT,

    -- What kind of event triggered this entry.
    event_type       TEXT        NOT NULL
        CHECK (event_type IN ('commit', 'refine', 'supersede', 'reinforce')),

    -- Why the belief changed (from the commit pipeline's reconciliation_reason).
    revision_reason  TEXT,

    -- Who / what produced the write (LLM name, tool, or 'local_user').
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
