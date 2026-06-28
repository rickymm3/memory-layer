-- Migration 036: AI-generated multi-response system for posts.
--
-- Each post can have multiple AI-synthesized responses, one per semantic
-- cluster of related memory atoms. Ordered by reach_score (engagement).
-- Reactions on individual responses feed their reach_score.

CREATE TABLE IF NOT EXISTS post_ai_responses (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id         UUID        NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    body            TEXT        NOT NULL,
    source_atom_ids UUID[]      NOT NULL DEFAULT '{}',
    reach_score     FLOAT       NOT NULL DEFAULT 0.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_post_ai_responses_post_id
    ON post_ai_responses(post_id);

CREATE TABLE IF NOT EXISTS post_response_reactions (
    response_id UUID        NOT NULL REFERENCES post_ai_responses(id) ON DELETE CASCADE,
    user_id     UUID        NOT NULL REFERENCES users(id)              ON DELETE CASCADE,
    vote        TEXT        NOT NULL CHECK (vote IN ('up', 'down')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (response_id, user_id)
);
