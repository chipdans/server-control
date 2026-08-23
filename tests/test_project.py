from __future__ import annotations

import sqlite3
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "desktop"))

from server_control_agent import (  # noqa: E402
    Agent,
    Config,
    EventBuffer,
    HubError,
    MinecraftStartupTracker,
    RconClient,
    is_rcon_lifecycle_log,
    parse_meminfo,
    parse_minecraft_player_list,
    parse_proc_diskstats,
    parse_proc_net_dev,
    parse_proc_stat_cpu,
)
from main import (  # noqa: E402
    display_bytes,
    display_duration,
    is_legacy_agent_network_error,
    is_rcon_lifecycle_message,
    minecraft_completion_candidates,
    replace_minecraft_completion,
)
from updater import is_newer, prepare_bootstrap_updater  # noqa: E402


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
        with self.assertRaises(PermissionError):
            agent._run_allowed_shell_command("uptime --version")

    def test_agent_event_buffer_is_bounded_and_reports_loss(self) -> None:
        buffer = EventBuffer(max_events=2, max_bytes=1_000)
        buffer.add("minecraft", "one")
        buffer.add("minecraft", "two")
        buffer.add("minecraft", "three")
        events = buffer.take()
        self.assertEqual([event["message"] for event in events], ["two", "three"])
        notice = buffer.take_overflow_notice()
        self.assertIsNotNone(notice)
        self.assertIn("пропущено строк: 1", str(notice.get("message")))

    def test_linux_metric_parsers(self) -> None:
        self.assertEqual(parse_proc_stat_cpu("cpu  10 2 3 20 5 0 0 0\n"), (40, 25))
        memory = parse_meminfo("MemTotal:       1024 kB\nMemAvailable:    256 kB\nSwapTotal:       512 kB\nSwapFree:        128 kB\n")
        self.assertEqual(memory["used_bytes"], 768 * 1024)
        self.assertEqual(memory["swap_free_bytes"], 128 * 1024)
        network = parse_proc_net_dev(
            "Inter-| Receive | Transmit\n lo: 10 0 0 0 0 0 0 0 20 0 0 0 0 0 0 0\n eth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n"
        )
        self.assertEqual(network, (100, 200))
        disk_io = parse_proc_diskstats(
            "8 0 sda 1 0 3 0 4 0 5 0\n8 1 sda1 1 0 999 0 1 0 999 0\n259 0 nvme0n1 1 0 7 0 4 0 11 0\n"
        )
        self.assertEqual(disk_io, ((3 + 7) * 512, (5 + 11) * 512))

    def test_dashboard_formatters(self) -> None:
        self.assertEqual(display_bytes(1024), "1.0 КиБ")
        self.assertEqual(display_bytes(2048, per_second=True), "2.0 КиБ/с")
        self.assertEqual(display_duration(3_661), "1 ч 1 мин")

    def test_rcon_collects_a_multi_packet_response(self) -> None:
        def packet(request_id: int, packet_type: int, payload: str) -> bytes:
            body = struct.pack("<ii", request_id, packet_type) + payload.encode("utf-8") + b"\x00\x00"
            return struct.pack("<i", len(body)) + body

        class FakeConnection:
            def __init__(self, response: bytes) -> None:
                self.response = response
                self.sent: list[bytes] = []

            def __enter__(self) -> "FakeConnection":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def close(self) -> None:
                return None

            def settimeout(self, _timeout: float) -> None:
                return None

            def sendall(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self, count: int) -> bytes:
                chunk, self.response = self.response[:count], self.response[count:]
                return chunk

        base_id = 123_456
        connection = FakeConnection(
            packet(base_id, 2, "")
            + packet(base_id + 1, 0, "first part ")
            + packet(base_id + 1, 0, "second part")
            + packet(base_id + 2, 0, "")
        )
        with patch("server_control_agent.socket.create_connection", return_value=connection), patch(
            "server_control_agent.time.time", return_value=123.456
        ):
            result = RconClient("127.0.0.1", 25575, "secret").command("help")
        self.assertEqual(result, "first part second part")
        self.assertEqual(len(connection.sent), 3)

    def test_rcon_reuses_one_authenticated_connection(self) -> None:
        def packet(request_id: int, packet_type: int, payload: str) -> bytes:
            body = struct.pack("<ii", request_id, packet_type) + payload.encode("utf-8") + b"\x00\x00"
            return struct.pack("<i", len(body)) + body

        class FakeConnection:
            def __init__(self, response: bytes) -> None:
                self.response = response
                self.sent: list[bytes] = []

            def settimeout(self, _timeout: float) -> None:
                return None

            def sendall(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self, count: int) -> bytes:
                chunk, self.response = self.response[:count], self.response[count:]
                return chunk

            def close(self) -> None:
                return None

        base_id = 123_456
        connection = FakeConnection(
            packet(base_id, 2, "")
            + packet(base_id + 1, 0, "first")
            + packet(base_id + 2, 0, "")
            + packet(base_id + 5, 0, "second")
            + packet(base_id + 6, 0, "")
        )
        with patch("server_control_agent.socket.create_connection", return_value=connection) as connect, patch(
            "server_control_agent.time.time", return_value=123.456
        ):
            client = RconClient("127.0.0.1", 25575, "secret")
            self.assertEqual(client.command("help"), "first")
            self.assertEqual(client.command("list"), "second")
        connect.assert_called_once()
        self.assertEqual(len(connection.sent), 5)

    def test_transient_console_noise_is_recognized(self) -> None:
        started = "[RCON Listener #1/INFO] Thread RCON Client /127.0.0.1 started"
        stopped = "[RCON Client /127.0.0.1 #42/INFO] Thread RCON Client /127.0.0.1 shutting down"
        self.assertTrue(is_rcon_lifecycle_log(started))
        self.assertTrue(is_rcon_lifecycle_message(stopped))
        self.assertFalse(is_rcon_lifecycle_log("[Server thread/ERROR] Failed to start Minecraft"))
        self.assertTrue(is_legacy_agent_network_error("[agent] Ошибка: Hub unavailable: Temporary failure in name resolution"))

    def test_hub_failures_are_coalesced_until_recovery(self) -> None:
        config = Config(
            hub_url="https://example.invalid",
            agent_api_key="test",
            poll_seconds=1,
            heartbeat_seconds=2,
            request_timeout_seconds=5,
            minecraft={},
            commands={"allow_shell_commands": ["uptime"]},
        )
        agent = Agent(config)
        with patch.object(agent, "_stderr"):
            agent._record_hub_failure(HubError("Hub unavailable: DNS"))
            agent._record_hub_failure(HubError("Hub unavailable: DNS"))
            agent._record_hub_recovered()
        messages = [event["message"] for event in agent.events.take(10)]
        self.assertEqual(len(messages), 2)
        self.assertIn("временно потеряна", messages[0])
        self.assertIn("Скрыто повторных сообщений: 1", messages[1])

    def test_update_archive_prepares_a_separate_bootstrap_updater(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "ServerControl"
            update = root / "update.zip"
            updater_name = "ServerControlUpdater.exe" if sys.platform == "win32" else "ServerControlUpdater"
            expected_bootstrap = f"{Path(updater_name).stem}.bootstrap{Path(updater_name).suffix}"
            with zipfile.ZipFile(update, "w") as archive:
                archive.writestr(updater_name, b"replacement-updater")
            bootstrap = prepare_bootstrap_updater(update, executable)
            self.assertEqual(bootstrap.name, expected_bootstrap)
            self.assertEqual(bootstrap.read_bytes(), b"replacement-updater")

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
