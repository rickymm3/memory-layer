-- Track how many times a stalled discussion has been re-routed.
-- Caps at 3 in reactivate_unresolved.py — after that, status moves to
-- 'dead' and the originating user receives a "still looking" notification.
ALTER TABLE discussions
  ADD COLUMN IF NOT EXISTS reactivation_count INTEGER NOT NULL DEFAULT 0;

-- Also add source_turn_text so drafts can show which chat turn generated them.
ALTER TABLE social_posts
  ADD COLUMN IF NOT EXISTS source_turn_text TEXT;
