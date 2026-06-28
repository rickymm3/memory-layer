-- 040: Per-user interest vector for personalized feed ranking.
-- Maintained as a decaying weighted centroid of posts the user has
-- viewed, reacted to, or contributed perspectives on.
-- Cold-start users (NULL) fall back to freshness+activity ranking.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS interest_vector vector;

-- Unique constraint on post_reach so we can upsert viewed/responded state.
-- post_reach rows previously had no uniqueness guarantee.
CREATE UNIQUE INDEX IF NOT EXISTS uq_post_reach_post_user
    ON post_reach(post_id, user_id)
    WHERE user_id IS NOT NULL;
