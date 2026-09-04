-- Avoid scanning the entire log history for retention cleanup.
CREATE INDEX IF NOT EXISTS idx_console_events_created ON console_events(created_at);
CREATE INDEX IF NOT EXISTS idx_commands_completed ON command_queue(completed_at)
  WHERE status IN ('completed','failed');
CREATE INDEX IF NOT EXISTS idx_commands_stale ON command_queue(status, claimed_at);
CREATE INDEX IF NOT EXISTS idx_jobs_stale ON jobs(status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_jobs_cancellations ON jobs(status, updated_at)
  WHERE cancel_requested=1;
