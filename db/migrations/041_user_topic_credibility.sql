-- 041: Per-user topic credibility scores.
-- A running weighted average of reaction signals on posts the user contributed to.
-- Upvote (+0.3) and downvote (-0.1) signals from post_reactions are attributed
-- to all contributors (post author + perspective authors) for each topic_tag.
-- Credibility emerges from breadth of associated atoms, not per-atom weighting.

CREATE TABLE IF NOT EXISTS user_topic_credibility (
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    topic_tag    TEXT        NOT NULL,
    score        FLOAT       NOT NULL DEFAULT 0.0,
    sample_count INT         NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, topic_tag)
);

CREATE INDEX IF NOT EXISTS idx_utc_user_id ON user_topic_credibility(user_id);
CREATE INDEX IF NOT EXISTS idx_utc_score   ON user_topic_credibility(score DESC);

-- Track which reactions have already been applied to credibility scores
-- so the worker can process each exactly once.
ALTER TABLE post_reactions
    ADD COLUMN IF NOT EXISTS credibility_applied BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_post_reactions_unapplied
    ON post_reactions(created_at)
    WHERE credibility_applied = false;
