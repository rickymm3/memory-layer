-- Migration 037: Prevent duplicate perspectives and enable async LLM processing.
--
-- The unique index on (post_id, author_user_id, md5(body)) means that if a
-- user submits the same text twice (e.g. double-click or network lag), the
-- second INSERT silently does nothing via ON CONFLICT DO NOTHING.
--
-- atom_id IS NULL marks perspectives that haven't been through the LLM
-- commit pipeline yet — the post_worker picks these up in the background.

CREATE UNIQUE INDEX IF NOT EXISTS perspectives_dedup
    ON perspectives(post_id, author_user_id, md5(body));
