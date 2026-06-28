-- Allow a perspective to also seed a standalone post.
-- When the perspective body is substantive (≥100 words), the worker
-- also calls generate_draft() and fills standalone_post_id.
-- The perspective then functions as both a reply to the parent post
-- AND the origin of its own new post.

ALTER TABLE perspectives
    ADD COLUMN IF NOT EXISTS standalone_post_id UUID REFERENCES social_posts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_perspectives_standalone_post
    ON perspectives(standalone_post_id)
    WHERE standalone_post_id IS NOT NULL;
