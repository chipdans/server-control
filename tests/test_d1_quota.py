from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import io
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
from api import ApiClient, ApiError


class D1QuotaTests(unittest.TestCase):
    def test_cleanup_uses_timestamp_index_and_keeps_recent_events(self):
        db = sqlite3.connect(":memory:")
        for path in sorted((ROOT / "worker/migrations").glob("*.sql")):
            db.executescript(path.read_text(encoding="utf-8"))
        db.executemany("INSERT INTO console_events(kind,message,created_at) VALUES('server','test',?)",
                       [(1,)] * 600 + [(100,)] * 10_000)
        sql = "DELETE FROM console_events WHERE id IN (SELECT id FROM console_events WHERE created_at<? ORDER BY created_at LIMIT 500)"
        plan = str(list(db.execute("EXPLAIN QUERY PLAN " + sql, (50,))))
        self.assertIn("idx_console_events_created", plan)
        db.execute(sql, (50,))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM console_events WHERE created_at=1").fetchone()[0], 100)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM console_events WHERE created_at=100").fetchone()[0], 10_000)
        # Idle queue polling must use indexes rather than scan completed history.
        for sql, params in [
            ("SELECT id FROM jobs WHERE cancel_requested=1 AND (status IN ('claimed','running') OR (status='cancelled' AND updated_at>?)) LIMIT 100", (50,)),
            ("SELECT id FROM command_queue WHERE status='pending' OR (status='claimed' AND claimed_at<?) ORDER BY created_at LIMIT 20", (50,)),
            ("SELECT id FROM jobs WHERE status='pending' OR (status IN ('claimed','running') AND heartbeat_at<?) ORDER BY created_at LIMIT 10", (50,)),
        ]:
            plan = str(list(db.execute("EXPLAIN QUERY PLAN " + sql, params)))
            self.assertNotIn("SCAN jobs", plan)
            self.assertNotIn("SCAN command_queue", plan)
        db.close()

    def test_client_waits_before_retrying_d1_but_recovers(self):
        class Response(io.BytesIO):
            status = 503
            def __init__(self):
                super().__init__(b'{"error":"d1_daily_quota_exceeded","message":"quota","retry_after":300}')

        class Connection:
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass

        with tempfile.TemporaryDirectory() as folder:
            client = ApiClient("https://test.invalid", Path(folder) / "client.log")
            with patch.object(client, "_resolve_ipv4_candidates", return_value=[]), \
                 patch.object(client, "_json_connection", return_value=Connection()) as connect, \
                 patch("api.time.monotonic", return_value=100):
                for _ in range(2):
                    with self.assertRaises(ApiError) as raised:
                        client.login("user", "password")
                    self.assertEqual(raised.exception.code, "d1_daily_quota_exceeded")
                self.assertEqual(connect.call_count, 1)
                with patch("api.time.monotonic", return_value=401):
                    with self.assertRaises(ApiError): client.login("user", "password")
                self.assertEqual(connect.call_count, 2)
            # Windows cannot remove a temporary directory with an open log.
            for handler in list(client._http_log.handlers):
                handler.close()
                client._http_log.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
