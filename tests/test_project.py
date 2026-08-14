from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "desktop"))

from server_control_agent import Agent, Config  # noqa: E402
from updater import is_newer  # noqa: E402


class ProjectTests(unittest.TestCase):
    def test_database_migration_runs_in_sqlite(self) -> None:
        connection = sqlite3.connect(":memory:")
        migration = (ROOT / "worker" / "migrations" / "0001_initial.sql").read_text(encoding="utf-8")
        connection.executescript(migration)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"users", "command_queue", "console_events", "agent_status", "audit_log"}.issubset(tables))

    def test_version_comparison(self) -> None:
        self.assertTrue(is_newer("v0.1.1", "0.1.0"))
        self.assertTrue(is_newer("v1.0.0", "0.9.9"))
        self.assertFalse(is_newer("v0.1.0", "0.1.0"))
        self.assertFalse(is_newer("v0.1.0", "0.1.1"))

    def test_agent_does_not_run_shell_chains(self) -> None:
        config = Config(
            hub_url="https://example.invalid",
            agent_api_key="test",
            poll_seconds=6,
            heartbeat_seconds=30,
            request_timeout_seconds=5,
            minecraft={},
            commands={"allow_shell_prefixes": ["uptime"]},
        )
        agent = Agent(config)
        with self.assertRaises(PermissionError):
            agent._run_allowed_shell_command("uptime; whoami")

    def test_agent_requires_allow_list(self) -> None:
        config = Config(
            hub_url="https://example.invalid",
            agent_api_key="test",
            poll_seconds=6,
            heartbeat_seconds=30,
            request_timeout_seconds=5,
            minecraft={},
            commands={"allow_shell_prefixes": ["uptime"]},
        )
        agent = Agent(config)
        with self.assertRaises(PermissionError):
            agent._run_allowed_shell_command("id")


if __name__ == "__main__":
    unittest.main()
