-- Migration 009: DB-backed dashboard chat sessions
-- Replaces Flask cookie session (4KB limit) with server-side JSONB storage.
-- The cookie holds only a UUID session_id; the full history lives here.

CREATE TABLE IF NOT EXISTS dashboard_sessions (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    history    JSONB       NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_updated_at
    ON dashboard_sessions (updated_at);
