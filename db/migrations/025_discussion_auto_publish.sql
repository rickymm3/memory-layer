-- Distinguish auto-published discussions (from low-confidence turns) from
-- manually clustered ones. Auto-published discussions appear in the explore
-- feed for all users to browse and react to.
ALTER TABLE discussions
    ADD COLUMN IF NOT EXISTS auto_published BOOLEAN NOT NULL DEFAULT false,
    -- Plain-text summary of the conversation that seeded this discussion.
    -- Shown in the explore feed — never the raw conversation.
    ADD COLUMN IF NOT EXISTS summary TEXT;

CREATE INDEX IF NOT EXISTS idx_discussions_auto_published
    ON discussions(auto_published, last_activity_at DESC)
    WHERE auto_published = true;
