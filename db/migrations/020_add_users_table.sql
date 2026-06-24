-- Migration 020: users table
--
-- Minimal user identity table for multi-user memory attribution.
-- source_user_id on memory_signals references this table (soft FK — no CASCADE
-- constraint yet so existing signal rows with free-form user IDs are not broken).
--
-- This is the identity anchor, not an authentication system.
-- Passwords, sessions, and OAuth belong in the dashboard web layer.

CREATE TABLE IF NOT EXISTS users (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username     TEXT        NOT NULL UNIQUE,
    email        TEXT        UNIQUE,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email    ON users(email);

-- Register the default local user so existing source_user_id='local_user' signals
-- can be resolved without breaking anything.
INSERT INTO users (id, username, display_name, created_at)
VALUES (gen_random_uuid(), 'local_user', 'Local User', now())
ON CONFLICT (username) DO NOTHING;
