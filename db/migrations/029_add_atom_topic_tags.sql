-- Migration 029: add topic_tags to memory_atoms so the affinity pipeline can
-- read keyword categories from atoms without joining to discussions.
-- Values are populated by the write pipeline critic review (suggested_topic_tags
-- field) and by the discussion_clusterer when tagging seed atoms.
ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS topic_tags TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_memory_atoms_topic_tags
    ON memory_atoms USING GIN (topic_tags);
