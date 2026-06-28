-- 039: Daily regen tokens for draft rewriting.
-- Each user gets 5 tokens per day; each manual "Rewrite" consumes 1.
-- The post_worker refreshes all users whose reset_at > 24h ago.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS regen_tokens          INT         NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS regen_tokens_reset_at TIMESTAMPTZ NOT NULL DEFAULT now();
