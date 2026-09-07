"""Exercise the actual embedded SSH program without requiring a Minecraft host."""

import ast
import builtins
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from direct_status import REMOTE_STATUS_PROGRAM


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(REMOTE_STATUS_PROGRAM)
        tree.body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
        cls.remote = {}
        exec(compile(tree, "<remote-status-test>", "exec"), cls.remote)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "latest.log"
        self.cache = self.root / "cache/readiness.json"
        self.log.write_text('[Server thread/INFO]: Done (14.132s)! For help, type "help"\n', encoding="utf-8")

    def evidence(self, session="boot:123:42", started=1, ping=False):
        return self.remote["startup_evidence"](self.log, session, started, ping, self.cache)

    def test_done_confirms_ready_when_status_ping_is_disabled(self):
        ready, lines = self.evidence()
        self.assertTrue(ready)
        self.assertEqual(self.remote["startup_from_log"](lines, [], ready, 123)["progress"], 100)

    def test_ready_poll_does_not_reread_large_log(self):
        self.assertTrue(self.evidence()[0])
        real_open = builtins.open

        def guarded(path, *args, **kwargs):
            if Path(path) == self.log:
                raise AssertionError("completed startup must not rescan the log")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", guarded):
            self.assertTrue(self.evidence()[0])

    def test_restart_does_not_reuse_old_done_or_cached_readiness(self):
        self.evidence()
        os.utime(self.log, (10, 10))
        self.assertFalse(self.evidence(session="boot:456:43", started=100)[0])
        self.log.write_text("Starting Minecraft server version 1.20.1\n", encoding="utf-8")
        self.assertFalse(self.evidence(session="boot:456:43", started=100)[0])

    def test_other_pack_does_not_inherit_ready_state(self):
        self.evidence()
        self.log = self.root / "other-pack.log"
        self.log.write_text("Loading mods\n", encoding="utf-8")
        self.assertFalse(self.evidence()[0])

    def test_truncated_or_replaced_log_invalidates_readiness(self):
        self.evidence()
        self.log.write_text("Loading\n", encoding="utf-8")
        self.assertFalse(self.evidence()[0])
        self.log.unlink()
        self.log.write_text("Preparing level \"world\"\n", encoding="utf-8")
        self.assertFalse(self.evidence()[0])

    def test_large_log_is_scanned_across_polls_and_partial_lines_are_preserved(self):
        line = b"[Server thread/INFO]: Loading an ordinary mod\n"
        with self.log.open("wb") as out:
            out.write(line * ((4 * 1024 * 1024 // len(line)) + 3))
            out.write(b'[Server thread/INFO]: Done (33.4s)! For help, type "help"\n')
        self.assertFalse(self.evidence()[0])
        self.assertTrue(self.evidence()[0])

    def test_ping_confirms_readiness_without_a_readable_log(self):
        self.log.unlink()
        self.assertTrue(self.evidence(ping=True)[0])
        self.assertFalse(self.evidence()[0])

    def test_server_ip_binding_is_used(self):
        props = self.root / "server.properties"
        props.write_text("server-ip=192.168.0.108\nserver-port=25566\n")
        self.assertEqual(self.remote["minecraft_host"](self.root), "192.168.0.108")
        self.assertEqual(self.remote["minecraft_port"](self.root, 25565), 25566)
        props.write_text("server-ip=0.0.0.0\n")
        self.assertEqual(self.remote["minecraft_host"](self.root), "127.0.0.1")

    def test_status_protocol_decodes_fragmented_response_and_rejects_oversized_frame(self):
        encode = self.remote["varint"]

        class Socket:
            def __init__(self, data):
                self.data = bytearray(data)

            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def sendall(self, value): pass
            def recv(self, count):
                piece = bytes(self.data[:min(count, 3)])
                del self.data[:len(piece)]
                return piece

        data = json.dumps({"version": {"name": "Forge"}, "players": {"online": 0, "max": 20}}).encode()
        packet = b"\x00" + encode(len(data)) + data
        with patch.object(self.remote["socket"], "create_connection", return_value=Socket(encode(len(packet)) + packet)):
            self.assertEqual(self.remote["minecraft_ping"](25565), (True, 0, 20))
        with patch.object(self.remote["socket"], "create_connection", return_value=Socket(encode(20 * 1024 * 1024))):
            self.assertEqual(self.remote["minecraft_ping"](25565), (False, None, None))


if __name__ == "__main__":
    unittest.main()
