-- 042: URL slugs for social_posts.
-- Adds a human-readable slug to each post for bookmarkable /post/<slug> URLs.
-- Generated from the title; duplicates get a numeric suffix (-2, -3, ...).

ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS slug TEXT;

-- Backfill: generate base_slug from title, append row-number suffix for dupes.
WITH base AS (
    SELECT id,
           RTRIM(
               LEFT(
                   REGEXP_REPLACE(
                       REGEXP_REPLACE(
                           LOWER(TRIM(title)),
                           '[^a-z0-9 ]', '', 'g'
                       ),
                       ' +', '-', 'g'
                   ),
               60),
           '-'
           ) AS base_slug,
           ROW_NUMBER() OVER (
               PARTITION BY
                   RTRIM(
                       LEFT(
                           REGEXP_REPLACE(
                               REGEXP_REPLACE(LOWER(TRIM(title)), '[^a-z0-9 ]', '', 'g'),
                               ' +', '-', 'g'
                           ), 60),
                       '-')
               ORDER BY created_at ASC
           ) AS rn
    FROM social_posts
    WHERE slug IS NULL
)
UPDATE social_posts sp
SET slug = CASE WHEN b.rn = 1 THEN b.base_slug
                ELSE b.base_slug || '-' || b.rn::text
           END
FROM base b
WHERE sp.id = b.id
  AND b.base_slug != '';

-- Fallback: any post still without a slug gets its short id
UPDATE social_posts
SET slug = LEFT(id::text, 8)
WHERE slug IS NULL OR slug = '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_social_posts_slug ON social_posts(slug);
