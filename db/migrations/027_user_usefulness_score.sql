-- Track per-user usefulness outcomes so expertise weighting can compound over time.
-- Incremented when a discussion where the user contributed is marked Validated.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS usefulness_score FLOAT NOT NULL DEFAULT 0.0;

-- Index for lookups in affinity and weighting queries
CREATE INDEX IF NOT EXISTS idx_users_usefulness_score ON users(usefulness_score DESC);
