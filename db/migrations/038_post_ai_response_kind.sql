-- Migration 038: Add response_kind to post_ai_responses.
--
-- synthesis : anchor response — the corpus's primary view on the post topic
-- dispute   : challenges or nuances the anchor synthesis
-- addition  : adds a new angle the synthesis didn't cover

ALTER TABLE post_ai_responses
    ADD COLUMN IF NOT EXISTS response_kind VARCHAR(20) NOT NULL DEFAULT 'synthesis'
        CHECK (response_kind IN ('synthesis', 'dispute', 'addition'));
