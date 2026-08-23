from __future__ import annotations

import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "desktop"))

from server_control_agent import (  # noqa: E402
    Agent,
    Config,
    EventBuffer,
    HubClient,
    HubError,
    LogTail,
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
from updater import _read_limited as read_update_response_limited  # noqa: E402
from apply_update import safe_extract as safe_extract_update  # noqa: E402
from api import ApiClient, ApiError  # noqa: E402
from state import AppState  # noqa: E402
from sc_agent.backups import BackupManager  # noqa: E402
from sc_agent.files import FileManager  # noqa: E402
from sc_agent.instances import InstanceProfile, InstanceStore, detect_pack  # noqa: E402
from sc_agent.jobs import JobExecutor, compatible_java_major, read_tail_bytes  # noqa: E402
from sc_agent.security import PathPolicy, SecurityError, safe_extract_zip, secure_path_within, validate_filename  # noqa: E402
from sc_agent.system import SystemInventory  # noqa: E402
from agent_update_helper import verify_manifest as verify_agent_manifest  # noqa: E402
from service_control_helper import validated_command  # noqa: E402


class ProjectTests(unittest.TestCase):
    def test_database_migration_runs_in_sqlite(self) -> None:
        connection = sqlite3.connect(":memory:")
        for migration_path in sorted((ROOT / "worker" / "migrations").glob("*.sql")):
            connection.executescript(migration_path.read_text(encoding="utf-8"))
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({
            "users", "command_queue", "console_events", "agent_status", "audit_log", "power_status",
            "jobs", "notifications", "notification_reads", "settings", "transfers", "transfer_parts",
        }.issubset(tables))
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("idx_jobs_active_lock", indexes)
        self.assertIn("idx_users_one_owner", indexes)
        self.assertIn("idx_notifications_user", indexes)
        notification_columns = {row[1] for row in connection.execute("PRAGMA table_info(notifications)")}
        self.assertIn("user_id", notification_columns)

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

    def test_root_service_helper_rechecks_exact_local_allow_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            instances = root / "instances.json"
            config.write_text(json.dumps({
                "allowed_services": ["ssh.service", "server-control-agent.service"],
                "minecraft": {"service": "legacy.service"},
            }), encoding="utf-8")
            instances.write_text(json.dumps({"instances": [{"id": "pack", "service": "server-control-minecraft@pack.service"}]}), encoding="utf-8")
            self.assertEqual(
                validated_command("restart", "ssh.service", config_path=config, instances_path=instances),
                ["/usr/bin/systemctl", "restart", "ssh.service"],
            )
            self.assertEqual(
                validated_command("kill", "server-control-minecraft@pack.service", config_path=config, instances_path=instances),
                ["/usr/bin/systemctl", "kill", "--signal=KILL", "server-control-minecraft@pack.service"],
            )
            for action, service in (("kill", "ssh.service"), ("restart", "server-control-agent.service"), ("start", "ssh.service --now")):
                with self.subTest(action=action, service=service), self.assertRaises(PermissionError):
                    validated_command(action, service, config_path=config, instances_path=instances)

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

    def test_large_logs_are_tailed_with_bounded_reads_and_complete_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.log"
            path.write_bytes(b"old line\n" * 300_000 + b"last complete\n")
            window, truncated = read_tail_bytes(path, 64 * 1024)
            self.assertTrue(truncated)
            self.assertLessEqual(len(window), 64 * 1024)
            self.assertTrue(window.endswith(b"last complete\n"))
            tail = LogTail(path, initial_lines=2)
            self.assertEqual(tail.read_new_lines()[-1], "last complete")
            with path.open("ab") as output:
                output.write(b"split")
            self.assertEqual(tail.read_new_lines(), [])
            with path.open("ab") as output:
                output.write(b" line\nnext\n")
            self.assertEqual(tail.read_new_lines(), ["split line", "next"])

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

    def test_agent_sync_falls_back_during_a_rolling_worker_deploy(self) -> None:
        config = Config(
            hub_url="https://example.invalid", agent_api_key="test", poll_seconds=1,
            heartbeat_seconds=2, request_timeout_seconds=5, minecraft={}, commands={},
        )
        client = HubClient(config)
        with patch.object(
            client,
            "request",
            side_effect=[HubError("Hub returned HTTP 404"), {"commands": [{"id": "legacy"}]}, {"jobs": [], "cancel": []}],
        ) as request:
            result = client.sync_work()
        self.assertEqual(result["commands"][0]["id"], "legacy")
        self.assertEqual(result["jobs"], [])
        self.assertEqual(request.call_count, 3)

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

    def test_update_extract_rejects_traversal_and_case_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            traversal = root / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../outside.exe", b"bad")
            with zipfile.ZipFile(traversal) as archive, self.assertRaises(RuntimeError):
                safe_extract_update(archive, root / "one")
            collision = root / "collision.zip"
            with zipfile.ZipFile(collision, "w") as archive:
                archive.writestr("Client.exe", b"one")
                archive.writestr("client.exe", b"two")
            with zipfile.ZipFile(collision) as archive, self.assertRaises(RuntimeError):
                safe_extract_update(archive, root / "two")

    def test_agent_manifest_rejects_unhashed_files_but_allows_compile_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            required = {
                "server_control_agent.py", "agent_update_helper.py", "service_control_helper.py", "instance_runner.py",
                "server-control-agent.service", "server-control-minecraft@.service",
                "servercontrol-sudoers.example", "sc_agent/__init__.py",
            }
            hashes: dict[str, str] = {}
            import hashlib
            for relative in required:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# verified\n", encoding="utf-8")
                hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            (root / "manifest.json").write_text(json.dumps({
                "schema": 1, "agent_version": "2.0.0", "release_tag": "v1.0.0", "files": hashes,
            }), encoding="utf-8")
            self.assertEqual(verify_agent_manifest(root), ("2.0.0", "v1.0.0"))
            # compileall created __pycache__; a repeated verification must
            # still succeed, while a new importable source file must not.
            self.assertEqual(verify_agent_manifest(root), ("2.0.0", "v1.0.0"))
            (root / "sc_agent/evil.py").write_text("raise RuntimeError\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                verify_agent_manifest(root)

    def test_http_response_readers_are_bounded(self) -> None:
        class Stream(io.BytesIO):
            headers: dict[str, str]

            def __init__(self, value: bytes, declared: str | None = None) -> None:
                super().__init__(value)
                self.headers = {} if declared is None else {"content-length": declared}

        self.assertEqual(ApiClient._read_limited(Stream(b"ok"), 2), b"ok")
        with self.assertRaises(ApiError):
            ApiClient._read_limited(Stream(b"too large"), 4)
        with self.assertRaises(RuntimeError):
            read_update_response_limited(Stream(b"x", "999"), 10)

    def test_path_policy_blocks_traversal_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "instance"
            root.mkdir()
            policy = PathPolicy(root)
            self.assertEqual(policy.resolve("config/server.properties"), root / "config" / "server.properties")
            for unsafe in ("../secret", "/etc/passwd", "C:\\Windows\\win.ini", "mods/../../secret"):
                with self.subTest(unsafe=unsafe), self.assertRaises(SecurityError):
                    policy.resolve(unsafe)
        self.assertEqual(validate_filename("server.properties"), "server.properties")
        for unsafe_name in ("../secret", "folder/file", "folder\\file", ".", ".."):
            with self.subTest(unsafe_name=unsafe_name), self.assertRaises((SecurityError, ValueError)):
                validate_filename(unsafe_name)

    @unittest.skipIf(os.name == "nt", "Windows CI may not permit unprivileged symlink creation")
    def test_path_policy_blocks_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "instance"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(SecurityError):
                PathPolicy(root).resolve("link/secret.txt")
            inside = root / "inside"
            inside.mkdir()
            (root / "internal-link").symlink_to(inside, target_is_directory=True)
            with self.assertRaises(SecurityError):
                PathPolicy(root).resolve("internal-link/file.txt")
            with self.assertRaises(SecurityError):
                secure_path_within(root, root / "internal-link/file.txt")

    def test_agent_zip_extract_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../../escaped.txt", b"bad")
            with self.assertRaises(SecurityError):
                safe_extract_zip(archive_path, root / "target")
            self.assertFalse((root / "escaped.txt").exists())

    def test_file_manager_paginates_and_prevents_lost_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(30):
                (root / f"file-{number:02d}.txt").write_text(str(number), encoding="utf-8")
            properties = root / "server.properties"
            properties.write_text("motd=one\n", encoding="utf-8")
            manager = FileManager(root)
            first = manager.list_directory("", page=1, page_size=25)
            second = manager.list_directory("", page=2, page_size=25)
            self.assertEqual(first["pages"], 2)
            self.assertEqual(len(first["entries"]), 25)
            self.assertEqual(len(second["entries"]), 6)
            opened = manager.read_text("server.properties")
            properties.write_text("motd=external\n", encoding="utf-8")
            os.utime(properties, ns=(opened["mtime_ns"] + 1_000_000, opened["mtime_ns"] + 1_000_000))
            with self.assertRaises(FileExistsError):
                manager.write_text("server.properties", "motd=mine\n", expected_mtime_ns=opened["mtime_ns"])
            current = manager.read_text("server.properties")
            saved = manager.write_text("server.properties", "motd=safe\n", expected_mtime_ns=current["mtime_ns"])
            self.assertTrue(saved["safety_backup"])
            self.assertEqual(properties.read_text(encoding="utf-8"), "motd=safe\n")
            with self.assertRaises(ValueError):
                manager.operation("duplicate", "file-00.txt", name="../escape")

    def test_instance_store_writes_a_secret_free_runner_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            minecraft_root = root / "minecraft"
            instance_root = minecraft_root / "pack"
            instance_root.mkdir(parents=True)
            store_path = root / "state" / "instances.json"
            store = InstanceStore(store_path, minecraft_root)
            store.put(InstanceProfile(
                id="pack", name="Pack", directory=str(instance_root), service="server-control-minecraft@pack.service",
                startup_reviewed=True, startup_command=["/usr/bin/java", "-jar", "server.jar", "nogui"],
                rcon_password="a-very-secret-password", managed_service=True,
            ))
            full = json.loads(store_path.read_text(encoding="utf-8"))
            runner_path = store_path.with_name("runner-instances.json")
            runner = json.loads(runner_path.read_text(encoding="utf-8"))
            self.assertEqual(full["instances"][0]["rcon_password"], "a-very-secret-password")
            self.assertNotIn("rcon_password", runner["instances"][0])
            self.assertEqual(runner["instances"][0]["id"], "pack")
            if os.name != "nt":
                self.assertEqual(store_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(runner_path.stat().st_mode & 0o777, 0o640)

    def test_instance_store_repairs_a_stale_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            minecraft_root = root / "minecraft"
            instance_root = minecraft_root / "pack"
            instance_root.mkdir(parents=True)
            store_path = root / "state" / "instances.json"
            store_path.parent.mkdir()
            profile = InstanceProfile(id="pack", name="Pack", directory=str(instance_root))
            store_path.write_text(json.dumps({"schema": 1, "selected": "missing", "instances": [profile.to_disk()]}), encoding="utf-8")
            store = InstanceStore(store_path, minecraft_root)
            self.assertEqual(store.selected_id, "pack")
            self.assertEqual(json.loads(store_path.read_text(encoding="utf-8"))["selected"], "pack")

    def test_modpack_detection_and_java_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = root / "libraries/net/neoforged/neoforge/20.4.1/unix_args.txt"
            args.parent.mkdir(parents=True)
            args.write_text("-Dexample=true", encoding="utf-8")
            detected = detect_pack(root)
            self.assertEqual(detected["startup_candidates"][0][1], "@libraries/net/neoforged/neoforge/20.4.1/unix_args.txt")
        self.assertEqual(compatible_java_major("1.16.5"), 8)
        self.assertEqual(compatible_java_major("1.17.1"), 16)
        self.assertEqual(compatible_java_major("1.20.1"), 17)
        self.assertEqual(compatible_java_major("1.20.5"), 21)

    def test_backup_excludes_recursive_data_and_enforces_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "server"
            source.mkdir()
            (source / "world.dat").write_bytes(b"world")
            (source / "backups").mkdir()
            (source / "backups/recursive.zip").write_bytes(b"skip")
            (source / ".server-control-history").mkdir()
            (source / ".server-control-history/old.bak").write_bytes(b"skip")
            manager = BackupManager(root / "backup-store")
            first = manager.create("pack", source)
            second = manager.create("pack", source)
            archive, _metadata = manager.resolve_archive("pack", first["id"])
            with zipfile.ZipFile(archive) as value:
                self.assertEqual(value.namelist(), ["world.dat"])
            protected = manager.enforce_retention("pack", {"keep_last": 1}, preserve_ids={first["id"], second["id"]})
            self.assertEqual(protected["remaining"], 2)
            pruned = manager.enforce_retention("pack", {"keep_last": 1})
            self.assertEqual(pruned["remaining"], 1)

    def test_crash_classifier_returns_an_actionable_reason(self) -> None:
        tracker = MinecraftStartupTracker()
        tracker.set_service_active(True)
        tracker.observe("java.lang.OutOfMemoryError: Java heap space")
        tracker.set_service_active(False)
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["state"], "CRASHED")
        self.assertEqual(snapshot["crash"]["code"], "out_of_memory")
        self.assertIn("RAM", snapshot["crash"]["solution"])

    def test_system_services_are_collected_with_one_systemctl_call(self) -> None:
        completed = SimpleNamespace(
            stdout=b"Id=alpha.service\nDescription=Alpha\nActiveState=active\nSubState=running\nMainPID=42\n\nId=beta.service\nDescription=Beta\nActiveState=inactive\nSubState=dead\nMainPID=0\n",
            stderr=b"", returncode=0,
        )
        with patch("sc_agent.system.subprocess.run", return_value=completed) as run:
            values = SystemInventory._services(["alpha.service", "beta.service", "../../bad.service"])
        run.assert_called_once()
        self.assertEqual([item["name"] for item in values], ["alpha.service", "beta.service"])
        self.assertEqual(values[0]["pid"], 42)

    def test_slow_inventory_refresh_does_not_block_agent_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inventory = SystemInventory(Path(directory), Path(directory) / "backups", cache_seconds=2)
            started = threading.Event()
            release = threading.Event()

            def slow_sizes(_profiles: list[object]) -> dict[str, object]:
                started.set()
                release.wait(1)
                return {"instances": [], "minecraft_total": 0, "backups": 0, "logs": 0}

            with patch.object(inventory, "_collect_sizes", side_effect=slow_sizes), patch.object(
                inventory, "_java_versions", return_value=[]
            ), patch.object(inventory, "_system_info", return_value={"hostname": "test"}), patch.object(
                inventory, "_services", return_value=[]
            ):
                before = time.monotonic()
                snapshot = inventory.snapshot([], [])
                elapsed = time.monotonic() - before
                self.assertLess(elapsed, 0.2)
                self.assertIn("processes", snapshot)
                self.assertTrue(started.wait(0.5))
                release.set()
                deadline = time.monotonic() + 2
                while inventory._slow_refreshing and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(inventory._slow_refreshing)
                self.assertEqual(inventory._system.get("hostname"), "test")

    def test_app_state_bounds_incremental_collections(self) -> None:
        state = AppState(user={"role": "user", "permissions": ["server.view"]})
        payload = {
            "events": [{"id": value, "message": str(value)} for value in range(10_050)],
            "jobs": [{"id": str(value), "created_at": value} for value in range(600)],
            "notifications": [{"id": value} for value in range(550)],
            "next_after": 10_050,
        }
        state.apply_sync(payload)
        self.assertEqual(len(state.events), 10_000)
        self.assertEqual(len(state.jobs), 500)
        self.assertEqual(len(state.notifications), 500)
        self.assertEqual(state.event_cursor, 10_050)

    def test_minecraft_command_result_is_not_published_to_global_console(self) -> None:
        events: list[tuple[object, ...]] = []
        executor = JobExecutor(
            hub=None,
            instances=None,  # type: ignore[arg-type]
            backups=None,  # type: ignore[arg-type]
            service_action=lambda *_args: {},
            instance_status=lambda _instance_id: {},
            minecraft_command=lambda _instance_id, _command: "private command result",
            server_action=lambda _action: {},
            service_control=lambda _service, _action: {},
            agent_update=lambda _payload: {},
            event=lambda *args, **_kwargs: events.append(args),
        )
        result = executor._dispatch(
            "job-private-rcon",
            "minecraft_command",
            {"instance_id": "main", "command": "list"},
            threading.Event(),
        )
        self.assertEqual(result["output"], "private command result")
        self.assertEqual(events, [])

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
