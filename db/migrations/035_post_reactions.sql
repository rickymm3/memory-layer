-- Migration 035: Lightweight post reactions (thumbs up / down).
--
-- Hidden from public display — counts are never shown. Used only as a
-- reach_score signal with lower weight than a perspective response.
-- Weights: up vote +0.3, down vote -0.1. A perspective adds ~1.0.
-- One vote per user per post (PRIMARY KEY enforces uniqueness).

CREATE TABLE IF NOT EXISTS post_reactions (
    post_id    UUID        NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    user_id    UUID        NOT NULL REFERENCES users(id)        ON DELETE CASCADE,
    vote       TEXT        NOT NULL CHECK (vote IN ('up', 'down')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_post_reactions_post_id ON post_reactions(post_id);
