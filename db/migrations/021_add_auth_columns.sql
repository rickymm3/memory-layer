-- Auth columns for multi-user hosted deployment.
-- Safe to apply on existing single-user installs — no data loss.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS api_token     TEXT UNIQUE DEFAULT gen_random_uuid()::text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin      BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active     BOOLEAN NOT NULL DEFAULT true;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_token ON users(api_token);

-- Existing local_user becomes admin so they can still access the admin dashboard.
UPDATE users SET is_admin = true WHERE username = 'local_user';
