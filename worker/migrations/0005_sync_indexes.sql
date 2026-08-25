-- Keep the high-frequency /v1/sync query on a bounded compound index as the
-- job and notification history grows over months of normal use.
CREATE INDEX IF NOT EXISTS idx_jobs_user_updated
ON jobs(requested_by, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_user_id
ON notifications(user_id, id DESC);
