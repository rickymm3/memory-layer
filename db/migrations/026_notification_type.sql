-- Add notification_type to user_notifications so Reopened and Answered events
-- can be distinguished from generic "new activity" notifications.
ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS notification_type TEXT NOT NULL DEFAULT 'reaction';
