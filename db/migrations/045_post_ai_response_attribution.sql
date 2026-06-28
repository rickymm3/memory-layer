-- Attribution for AI-generated post responses.
-- Stores the distinct contributor usernames whose atoms drove the response,
-- ordered by importance. Used for UI credit display without re-querying.

ALTER TABLE post_ai_responses
    ADD COLUMN IF NOT EXISTS contributing_usernames TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN post_ai_responses.contributing_usernames IS
    'Ordered list of contributor usernames whose atoms drove this response (high-imp first)';
