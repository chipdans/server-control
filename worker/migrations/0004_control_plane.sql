PRAGMA foreign_keys = ON;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_one_owner ON users(role) WHERE role = 'owner';

-- Long-running operations are separate from the legacy command queue so an
-- existing 0.3.x client and agent continue to work during a rolling update.
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  requested_by TEXT,
  instance_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending', 'claimed', 'running', 'completed', 'failed', 'cancelled')),
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  stage TEXT,
  message TEXT,
  result TEXT,
  error_code TEXT,
  lock_key TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
  claimed_at INTEGER,
  heartbeat_at INTEGER,
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  completed_at INTEGER,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY(requested_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_poll ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(requested_by, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_lock
ON jobs(lock_key)
WHERE lock_key IS NOT NULL AND status IN ('pending', 'claimed', 'running');

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT,
  type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('info', 'success', 'warning', 'error')),
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  target TEXT,
  created_at INTEGER NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, id DESC);

CREATE TABLE IF NOT EXISTS notification_reads (
  notification_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  read_at INTEGER NOT NULL,
  PRIMARY KEY(notification_id, user_id),
  FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_by TEXT,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY(updated_by) REFERENCES users(id)
);

-- R2 stores bytes; D1 stores only ownership, progress and multipart metadata.
CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY,
  direction TEXT NOT NULL CHECK(direction IN ('upload', 'download')),
  requested_by TEXT,
  instance_id TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  file_name TEXT NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  overwrite INTEGER NOT NULL DEFAULT 0 CHECK(overwrite IN (0, 1)),
  status TEXT NOT NULL CHECK(status IN ('created', 'uploading', 'ready', 'importing', 'completed', 'failed', 'cancelled')),
  multipart_upload_id TEXT,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  FOREIGN KEY(requested_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_transfers_user ON transfers(requested_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transfers_expiry ON transfers(expires_at);

CREATE TABLE IF NOT EXISTS transfer_parts (
  transfer_id TEXT NOT NULL,
  part_number INTEGER NOT NULL,
  etag TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  PRIMARY KEY(transfer_id, part_number),
  FOREIGN KEY(transfer_id) REFERENCES transfers(id) ON DELETE CASCADE
);

ALTER TABLE console_events ADD COLUMN instance_id TEXT;
ALTER TABLE console_events ADD COLUMN source TEXT NOT NULL DEFAULT '';
ALTER TABLE console_events ADD COLUMN level TEXT NOT NULL DEFAULT 'INFO';

ALTER TABLE audit_log ADD COLUMN target TEXT;
ALTER TABLE audit_log ADD COLUMN result TEXT NOT NULL DEFAULT 'success';
ALTER TABLE audit_log ADD COLUMN ip TEXT;
ALTER TABLE audit_log ADD COLUMN device TEXT;
