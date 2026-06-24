-- Add partial unique constraint so reaction notifications can be upserted/batched.
-- Only one unread notification per (user, discussion) pair is kept — additional
-- reactions increment new_atom_count on the existing row rather than flooding
-- the notification inbox. The constraint is partial (WHERE read = false) so
-- historical read notifications are preserved for audit/history.
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_notifications_unread_unique
    ON user_notifications (user_id, discussion_id)
    WHERE read = false;
