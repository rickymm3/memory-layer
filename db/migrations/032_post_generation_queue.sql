-- Post generation queue: atoms enqueued for background post generation.
-- Worker clusters pending items by scope and generates one post per cluster.
CREATE TABLE IF NOT EXISTS post_generation_queue (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username     TEXT        NOT NULL,
    atom_id      UUID        NOT NULL,
    scope        TEXT,
    enqueued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status       TEXT        NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    attempts     INT         NOT NULL DEFAULT 0,
    last_error   TEXT,
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pgq_status_enqueued ON post_generation_queue(status, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_pgq_username        ON post_generation_queue(username);

-- Track which users have been shown a post (heartbeat distribution).
-- Feeds the "might interest you" surface and promotion-to-popular logic.
CREATE TABLE IF NOT EXISTS post_reach (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id    UUID        NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    user_id    UUID        REFERENCES users(id) ON DELETE SET NULL,
    shown_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    viewed     BOOLEAN     NOT NULL DEFAULT false,
    responded  BOOLEAN     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_post_reach_post_id ON post_reach(post_id);
CREATE INDEX IF NOT EXISTS idx_post_reach_user_id ON post_reach(user_id);

-- Reach score on social_posts: rises with views, triggers /popular promotion.
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS reach_score FLOAT NOT NULL DEFAULT 0.0;
CREATE INDEX IF NOT EXISTS idx_social_posts_reach ON social_posts(reach_score DESC);
