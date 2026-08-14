-- A short-lived local cache prevents a slow smart-home API from blocking the UI.
CREATE TABLE IF NOT EXISTS power_status (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  on_state INTEGER CHECK(on_state IN (0, 1)),
  online_state INTEGER CHECK(online_state IN (0, 1)),
  updated_at INTEGER NOT NULL
);
