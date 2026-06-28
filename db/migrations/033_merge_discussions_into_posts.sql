-- 033: Merge discussions → social_posts
-- One content type. Perspectives replace discussion reactions.
-- user_notifications.discussion_id → post_id.

-- 1. Extend social_posts
ALTER TABLE social_posts
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'generated',
    ADD COLUMN IF NOT EXISTS perspective_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Temporary column for id mapping during migration
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS legacy_discussion_id UUID;

-- 2. Migrate discussions → social_posts
INSERT INTO social_posts (
    title, body, format, status, author_user_id,
    primary_atom_ids, topic_tags, source, perspective_count,
    last_activity_at, created_at, published_at,
    confidence_at_publish, legacy_discussion_id
)
SELECT
    d.title,
    COALESCE(NULLIF(TRIM(COALESCE(d.summary, '')), ''),
             'Discussion thread on: ' || d.title) AS body,
    'discussion'::text AS format,
    CASE WHEN d.auto_published THEN 'published' ELSE 'archived' END AS status,
    d.created_by_user_id,
    d.seed_atom_ids,
    d.topic_tags,
    'imported'::text AS source,
    d.contributor_count,
    d.last_activity_at,
    d.created_at,
    CASE WHEN d.auto_published THEN d.last_activity_at ELSE NULL END AS published_at,
    NULL AS confidence_at_publish,
    d.id AS legacy_discussion_id
FROM discussions d
ON CONFLICT DO NOTHING;

-- 3. Migrate discussion_atoms → post_contributors
INSERT INTO post_contributors (post_id, user_id, contribution_type, atom_ids, created_at)
SELECT
    sp.id                                                          AS post_id,
    (SELECT id FROM users WHERE username = da.source_user_id LIMIT 1) AS user_id,
    'discussion'::text                                             AS contribution_type,
    ARRAY[da.atom_id]                                             AS atom_ids,
    da.added_at                                                   AS created_at
FROM discussion_atoms da
JOIN social_posts sp ON sp.legacy_discussion_id = da.discussion_id
ON CONFLICT DO NOTHING;

-- 4. Perspectives table (unified response surface — no MCP required)
CREATE TABLE IF NOT EXISTS perspectives (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id         UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    author_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    author_username TEXT,
    body            TEXT NOT NULL,
    atom_id         UUID REFERENCES memory_atoms(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_perspectives_post_id ON perspectives(post_id);
CREATE INDEX IF NOT EXISTS idx_perspectives_author ON perspectives(author_user_id);

-- 5. Rebuild user_notifications with post_id instead of discussion_id
DROP TABLE IF EXISTS user_notifications CASCADE;
CREATE TABLE user_notifications (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id           UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    new_atom_count    INT  NOT NULL DEFAULT 1,
    read              BOOLEAN NOT NULL DEFAULT false,
    notification_type TEXT NOT NULL DEFAULT 'new_perspective',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_notifications_user_post_unread
    ON user_notifications(user_id, post_id) WHERE read = false;
CREATE INDEX IF NOT EXISTS idx_user_notifications_user_unread
    ON user_notifications(user_id, read) WHERE read = false;

-- 6. Drop old tables (discussion_atoms FK to discussions must go first)
DROP TABLE IF EXISTS discussion_atoms CASCADE;
DROP TABLE IF EXISTS discussions CASCADE;

-- 7. Remove temporary mapping column
ALTER TABLE social_posts DROP COLUMN IF EXISTS legacy_discussion_id;

-- 8. Index for activity feed ordering
CREATE INDEX IF NOT EXISTS idx_social_posts_last_activity
    ON social_posts(last_activity_at DESC);
