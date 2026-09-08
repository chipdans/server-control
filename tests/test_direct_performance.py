"""Real embedded telemetry: JVM attribution, bounded RCON and fresh samples."""

import ast
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from direct_status import REMOTE_STATUS_PROGRAM, dashboard_envelope
from pages_dashboard_v2 import minecraft_diagnostics


def packet(request_id, packet_type, payload=""):
    data = payload.encode() + b"\0\0"
    return struct.pack("<iii", len(data) + 8, request_id, packet_type) + data


class FragmentedSocket:
    def __init__(self, data):
        self.data, self.sent, self.timeouts = bytearray(data), [], []

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, value): self.timeouts.append(value)
    def sendall(self, value): self.sent.append(value)
    def recv(self, count):
        result = bytes(self.data[:min(count, 3)])
        del self.data[:len(result)]
        return result


@unittest.skipIf(os.name == "nt", "The embedded program runs on Debian; tested by the Ubuntu release gate")
class TelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(REMOTE_STATUS_PROGRAM)
        tree.body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
        cls.remote = {}
        exec(compile(tree, "<remote-telemetry-test>", "exec"), cls.remote)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache/telemetry.json"
        self.profile = {"directory": str(self.root), "loader": "Forge 47.4.0"}
        self.sample = {"pid": 42, "start_ticks": 100, "ticks": 0, "name": "java", "memory_bytes": 2 * 1024 ** 3}

    def collect(self, state="RUNNING", session="boot:123:42"):
        return self.remote["collect_minecraft_telemetry"](
            {"MainPID": "40"}, self.profile, state, session, "127.0.0.1", [], self.cache,
        )

    def test_jvm_in_service_cgroup_is_found_even_when_tmux_detaches(self):
        proc, cgroup = self.root / "proc", self.root / "cgroup"
        group = cgroup / "system.slice/minecraft.service"
        group.mkdir(parents=True)
        (group / "cgroup.procs").write_text("40\n42\n")
        for pid, name in ((40, "python (wrapper)"), (42, "java"), (999, "java")):
            directory = proc / str(pid)
            directory.mkdir(parents=True)
            fields = ["0"] * 22
            fields[0], fields[1] = "S", "1"
            fields[11], fields[12], fields[19], fields[21] = "100", "20", "500", "1024"
            (directory / "stat").write_text(f"{pid} ({name}) " + " ".join(fields))
        sample = self.remote["find_java_process"]({"MainPID": "40", "ControlGroup": "/system.slice/minecraft.service"}, proc, cgroup)
        self.assertEqual(sample["pid"], 42)
        self.assertEqual(sample["ticks"], 120)
        self.assertEqual(sample["memory_bytes"], 1024 * os.sysconf("SC_PAGE_SIZE"))
        self.assertIsNone(self.remote["find_java_process"]({"MainPID": "40"}, proc, cgroup))
        # The wrapper fallback follows only its own descendants.
        children = proc / "40/task/40/children"
        children.parent.mkdir(parents=True)
        children.write_text("42")
        self.assertEqual(self.remote["find_java_process"]({"MainPID": "40"}, proc, cgroup)["pid"], 42)

    def test_queries_are_shared_for_30_seconds_and_restart_clears_cpu_baseline(self):
        with patch.dict(self.remote, find_java_process=lambda *_: dict(self.sample)), patch.object(self.remote["time"], "monotonic", return_value=10) as clock, patch.object(self.remote["os"], "cpu_count", return_value=4), patch.dict(self.remote, measure_performance=lambda *_: {"status": "ok", "tps": 20.0, "mspt": 10.0}):
            process, performance = self.collect()
            self.assertIsNone(process["cpu_percent"])
            self.assertEqual(performance["tps"], 20)
            clock.return_value = 20
            self.sample["ticks"] = 4 * os.sysconf("SC_CLK_TCK")
            with patch.dict(self.remote, measure_performance=lambda *_: self.fail("A second client must use the cached TPS")):
                process, performance = self.collect()
            self.assertEqual(process["cpu_percent"], 10.0)
            self.assertEqual(performance["age_seconds"], 10)
            clock.return_value = 41
            with patch.dict(self.remote, measure_performance=lambda *_: {"status": "timeout"}):
                process, performance = self.collect()
            self.assertEqual(performance["status"], "timeout")
            self.assertNotIn("tps", performance)
            self.sample["start_ticks"] = 600  # PID reuse, even before systemd refreshes.
            process, performance = self.collect(session="boot:456:42")
            self.assertIsNone(process["cpu_percent"])
            self.assertEqual(performance["status"], "ok")
            process, performance = self.collect(state="STOPPED")
            self.assertIsNone(process["memory_bytes"])
            self.assertNotIn("tps", performance)

    def test_pack_switch_and_unsupported_command_do_not_reuse_stale_measurements(self):
        with patch.dict(self.remote, find_java_process=lambda *_: dict(self.sample)), patch.object(self.remote["time"], "monotonic", return_value=1000) as clock:
            with patch.dict(self.remote, measure_performance=lambda *_: {"status": "ok", "tps": 20, "mspt": 12}):
                self.collect()
            self.profile["directory"] = str(self.root / "other-pack")
            with patch.dict(self.remote, measure_performance=lambda *_: {"status": "unsupported"}):
                self.assertEqual(self.collect()[1]["status"], "unsupported")
            clock.return_value = 1040
            with patch.dict(self.remote, measure_performance=lambda *_: self.fail("Unsupported command must back off")):
                self.assertNotIn("tps", self.collect()[1])

    def test_fragmented_rcon_uses_complete_overall_summary_not_dimension_tps(self):
        stream = FragmentedSocket(
            packet(1, 0) + packet(1, 2)
            + packet(10, 0, "Dim minecraft:overworld: Mean tick time: 1.000 ms. Mean TPS: 20.000\n")
            + packet(10, 0, "§7Overall: Mean tick time: 80,000 ms. ")
            + packet(10, 0, "Mean TPS: 12,500") + packet(11, 0)
        )
        with patch.object(self.remote["socket"], "create_connection", return_value=stream):
            result = self.remote["rcon_performance"]("127.0.0.1", 25575, "secret", "Forge")
        self.assertEqual((result["tps"], result["mspt"]), (12.5, 80))
        self.assertTrue(all(0 < value <= 3 for value in stream.timeouts))
        self.assertIsNone(self.remote["parse_forge_performance"]("Dim mod:world: Mean tick time: 1 ms. Mean TPS: 20"))

    def test_rcon_authentication_frame_size_and_request_ids_are_checked(self):
        for data, error in (
            (packet(-1, 2), PermissionError),
            (struct.pack("<i", 100000000), ValueError),
            (packet(1, 2) + packet(999, 0, "Overall: Mean tick time: 1 ms. Mean TPS: 20"), ValueError),
        ):
            with self.subTest(error=error, data=data[:4]):
                stream = FragmentedSocket(data)
                with patch.object(self.remote["socket"], "create_connection", return_value=stream):
                    with self.assertRaises(error):
                        self.remote["rcon_performance"]("127.0.0.1", 25575, "secret", "Forge")
                if error is PermissionError:
                    self.assertEqual(len(stream.sent), 1)

    def test_timeout_and_disabled_rcon_leave_other_metrics_and_secrets_intact(self):
        properties = self.root / "server.properties"
        secret = "p:as=s\\word"
        original = "enable-rcon=true\nrcon.port=25575\nrcon.password=p\\:as\\=s\\\\word\n"
        properties.write_text(original)
        self.assertEqual(self.remote["rcon_properties"](self.root)["rcon.password"], secret)
        with patch.dict(self.remote, find_java_process=lambda *_: dict(self.sample)), patch.object(self.remote["socket"], "create_connection", side_effect=TimeoutError):
            process, performance = self.collect()
        self.assertEqual(process["memory_bytes"], self.sample["memory_bytes"])
        self.assertEqual(performance["status"], "timeout")
        envelope = dashboard_envelope({"minecraft": {"process": process, "performance": performance}})
        self.assertEqual(envelope["status"]["minecraft"]["performance"]["status"], "timeout")
        self.assertNotIn(secret, json.dumps(envelope) + self.cache.read_text())
        self.assertEqual(properties.read_text(), original)
        properties.write_text("enable-rcon=false\n")
        with patch.object(self.remote["socket"], "create_connection", side_effect=AssertionError("Disabled RCON must not connect")):
            self.assertEqual(self.remote["measure_performance"](self.root, "127.0.0.1", [], "Forge")["status"], "disabled")
        properties.write_text(original)
        with patch.object(self.remote["socket"], "create_connection", side_effect=AssertionError("No credentials to nonlocal endpoints")):
            self.assertEqual(self.remote["measure_performance"](self.root, "203.0.113.1", [], "Forge")["status"], "unavailable")


class DiagnosticMessageTests(unittest.TestCase):
    def test_slow_healthy_and_outdated_samples_are_distinguished(self):
        instance = {"state": "RUNNING", "performance": {"status": "ok", "tps": 12.5, "mspt": 80, "age_seconds": 0}}
        self.assertEqual(minecraft_diagnostics(instance, {}, True)["tone"], "danger")
        instance["performance"].update(tps=20, mspt=15)
        self.assertEqual(minecraft_diagnostics(instance, {}, True)["tone"], "success")
        instance["performance"]["age_seconds"] = 90
        self.assertIsNone(minecraft_diagnostics(instance, {}, True)["tps"])
        instance["performance"]["age_seconds"] = 0
        self.assertIsNone(minecraft_diagnostics(instance, {}, False)["tps"])
        instance["state"] = "STARTING"
        self.assertIsNone(minecraft_diagnostics(instance, {}, True)["tps"])

    def test_limited_memory_and_unavailable_rcon_are_not_diagnosed_as_cpu_lag(self):
        instance = {"state": "RUNNING", "performance": {"status": "disabled"}, "process": {"cpu_percent": 99}}
        result = minecraft_diagnostics(instance, {}, True)
        self.assertIn("RCON", result["message"])
        self.assertIsNone(result["tps"])
        self.assertEqual(result["process"]["cpu_percent"], 99)
        result = minecraft_diagnostics(instance, {"memory": {"percent": 95}}, True)
        self.assertIn("мало свободной памяти", result["message"])


if __name__ == "__main__":
    unittest.main()
