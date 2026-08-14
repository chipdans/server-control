-- Only one graceful power-off request may be active at a time.
-- Finished requests remain in the audit history and do not block future ones.
CREATE UNIQUE INDEX IF NOT EXISTS idx_command_queue_one_active_safe_power_off
ON command_queue(type)
WHERE type = 'safe_power_off' AND status IN ('pending', 'claimed');
