-- Migration 017: Memory compaction support
--
-- Adds:
--   - 'evidence' lifecycle status: atoms subsumed into a belief, kept as
--     provenance but removed from active retrieval
--   - peak_confidence / peak_support_weight: high-water marks recorded at
--     demotion time so historical framing can say "was held with confidence
--     0.87 and 8 reinforcing signals" rather than just current values
--   - 'supports_belief' relation type: links evidence atoms to their belief
--   - 'compact' event type in belief_revision_log: distinguishes compaction
--     events from organic supersession

-- 1. lifecycle_status: add 'evidence'
ALTER TABLE memory_atoms
    DROP CONSTRAINT IF EXISTS memory_atoms_lifecycle_status_check;
ALTER TABLE memory_atoms
    ADD CONSTRAINT memory_atoms_lifecycle_status_check
    CHECK (lifecycle_status = ANY (ARRAY[
        'active', 'superseded', 'deprecated', 'archived', 'contested', 'evidence'
    ]));

-- 2. peak values recorded at demotion time
ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS peak_confidence     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS peak_support_weight DOUBLE PRECISION;

-- 3. memory_atom_relations: add 'supports_belief'
ALTER TABLE memory_atom_relations
    DROP CONSTRAINT IF EXISTS memory_atom_relations_relation_type_check;
ALTER TABLE memory_atom_relations
    ADD CONSTRAINT memory_atom_relations_relation_type_check
    CHECK (relation_type = ANY (ARRAY[
        'supports', 'contradicts', 'specializes', 'generalizes', 'related',
        'supports_belief'
    ]));

-- 4. belief_revision_log: add 'compact' event type
ALTER TABLE belief_revision_log
    DROP CONSTRAINT IF EXISTS belief_revision_log_event_type_check;
ALTER TABLE belief_revision_log
    ADD CONSTRAINT belief_revision_log_event_type_check
    CHECK (event_type = ANY (ARRAY[
        'commit', 'refine', 'supersede', 'reinforce', 'compact'
    ]));

-- Index: fast lookup of evidence atoms for a belief via supports_belief edges
CREATE INDEX IF NOT EXISTS idx_atom_relations_belief
    ON memory_atom_relations (atom_b_id)
    WHERE relation_type = 'supports_belief';
