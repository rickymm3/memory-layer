-- Social layer: posts and contributor attribution.
-- Depends on: users table (020), visibility column (018), auth columns (021).
CREATE TABLE IF NOT EXISTS social_posts (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title                TEXT        NOT NULL,
    body                 TEXT        NOT NULL,
    format               TEXT        NOT NULL DEFAULT 'article'
                             CHECK (format IN ('tutorial','article','discussion','open_question','news_brief','narrative')),
    status               TEXT        NOT NULL DEFAULT 'draft'
                             CHECK (status IN ('draft','published','archived')),
    author_user_id       UUID        REFERENCES users(id) ON DELETE SET NULL,
    primary_atom_ids     UUID[]      NOT NULL DEFAULT '{}',
    topic_tags           TEXT[]      NOT NULL DEFAULT '{}',
    confidence_at_publish FLOAT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at         TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS post_contributors (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id           UUID        NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    user_id           UUID        REFERENCES users(id) ON DELETE SET NULL,
    contribution_type TEXT        NOT NULL DEFAULT 'discussion'
                          CHECK (contribution_type IN ('primary','correction','evidence','discussion','photo')),
    atom_ids          UUID[]      NOT NULL DEFAULT '{}',
    quote             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_social_posts_status        ON social_posts(status);
CREATE INDEX IF NOT EXISTS idx_social_posts_author        ON social_posts(author_user_id);
CREATE INDEX IF NOT EXISTS idx_social_posts_published_at  ON social_posts(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_post_contributors_post_id  ON post_contributors(post_id);
CREATE INDEX IF NOT EXISTS idx_post_contributors_user_id  ON post_contributors(user_id);
