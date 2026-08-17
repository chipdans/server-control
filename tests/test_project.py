from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "desktop"))

from server_control_agent import Agent, Config, MinecraftStartupTracker, parse_minecraft_player_list  # noqa: E402
from main import minecraft_completion_candidates, replace_minecraft_completion  # noqa: E402
from updater import is_newer  # noqa: E402


class ProjectTests(unittest.TestCase):
    def test_database_migration_runs_in_sqlite(self) -> None:
        connection = sqlite3.connect(":memory:")
        for migration_path in sorted((ROOT / "worker" / "migrations").glob("*.sql")):
            connection.executescript(migration_path.read_text(encoding="utf-8"))
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"users", "command_queue", "console_events", "agent_status", "audit_log", "power_status"}.issubset(tables))

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

    def test_startup_tracker_uses_real_forge_and_minecraft_markers(self) -> None:
        tracker = MinecraftStartupTracker()
        tracker.set_service_active(True)
        tracker.observe("[main/INFO] [cpw.mods.modlauncher.Launcher/MODLAUNCHER]: ModLauncher running")
        self.assertEqual(tracker.phase, "starting_java")
        tracker.observe("[main/INFO] [net.minecraftforge.fml.loading.moddiscovery.ModDiscoverer/LOADING]: Found mod file example.jar")
        self.assertEqual(tracker.phase, "loading_mods")
        tracker.observe("[Server thread/INFO] [net.minecraft.server.MinecraftServer/]: Preparing level \"world\"")
        self.assertEqual(tracker.phase, "loading_world")
        tracker.observe("[Worker-Main-1/INFO] [net.minecraft.server.level.progress.LoggerChunkProgressListener/]: Preparing spawn area: 50%")
        self.assertEqual(tracker.phase, "preparing_spawn")
        self.assertGreaterEqual(tracker.progress, 90)
        tracker.observe('[Server thread/INFO] [net.minecraft.server.dedicated.DedicatedServer/]: Done (4.306s)! For help, type "help"')
        self.assertTrue(tracker.ready)
        self.assertEqual(tracker.progress, 100)

    def test_player_list_and_command_completion(self) -> None:
        online, maximum, players = parse_minecraft_player_list("There are 2 of a max of 20 players online: Alice, Bob")
        self.assertEqual((online, maximum, players), (2, 20, ["Alice", "Bob"]))
        self.assertIn("/gamemode", minecraft_completion_candidates("/gam", ["home"], players))
        self.assertIn("/gamerule", minecraft_completion_candidates("/gam", ["home"], players))
        self.assertIn("Alice", minecraft_completion_candidates("tp A", [], players))
        self.assertIn("creative", minecraft_completion_candidates("gamemode c", [], players))
        self.assertEqual(replace_minecraft_completion("/gam", "/gamemode"), "/gamemode")
        self.assertEqual(replace_minecraft_completion("tp A", "Alice"), "tp Alice")


if __name__ == "__main__":
    unittest.main()
