import json
from pathlib import Path
from unittest.mock import patch

from support import IsolatedTest


class FeedTests(IsolatedTest):
    def test_name_required(self):
        for value in (None, [], "job", {}, {"name": ""}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "name"):
                self.server.write_feed(value)
        self.assertFalse(Path(self.server.FEEDS_DIR).exists())

    def test_progress_clamped_and_state_lowercased(self):
        for value, expected in [(-5, 0), (3, 1), ("0.25", 0.25), (None, None), ("bad", None)]:
            with self.subTest(value=value):
                row = self.server.write_feed({"name": "job", "state": "RUNNING", "progress": value})
                self.assertEqual(row["progress"], expected)
                self.assertEqual(row["state"], "running")

    def test_write_replaces_complete_json_atomically(self):
        server = self.server
        server.write_feed({"name": "job", "log": "old"})
        target = Path(server.FEEDS_DIR) / "job.json"
        replace = server.os.replace

        def observe(src, dst):
            self.assertEqual(json.loads(target.read_text())["log"], "old")
            self.assertEqual(json.loads(Path(src).read_text())["log"], "new")
            self.assertEqual(Path(src).parent, target.parent)
            replace(src, dst)

        with patch.object(server.os, "replace", side_effect=observe) as atomic:
            row = server.write_feed({"name": "job", "log": "new"})
        atomic.assert_called_once()
        self.assertEqual(json.loads(target.read_text()), row)
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_failed_replace_preserves_previous_feed(self):
        self.server.write_feed({"name": "job", "log": "old"})
        with patch.object(self.server.os, "replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                self.server.write_feed({"name": "job", "log": "new"})
        path = Path(self.server.FEEDS_DIR) / "job.json"
        self.assertEqual(json.loads(path.read_text())["log"], "old")

    def test_name_cannot_escape_feed_directory(self):
        self.server.write_feed({"name": "../../escape"})
        self.assertEqual(len(list(Path(self.server.FEEDS_DIR).glob("*.json"))), 1)
        self.assertFalse((self.home / "escape.json").exists())

    def seed(self, name, state, age, now=2_000_000):
        self.write_json(Path(self.server.FEEDS_DIR) / (name + ".json"),
                        {"name": name, "state": state, "updated": now - age, "log": "step", "progress": 0.4})

    def test_state_mapping(self):
        expected = {"running": "Running", "queued": "Queued", "done": "Ready", "ok": "Ready",
                    "failed": "Failed", "error": "Failed", "idle": "Ready", "unknown": "Ready"}
        for state in expected:
            self.seed(state, state.upper(), 1)
        with patch.object(self.server.time, "time", return_value=2_000_000):
            rows = self.server.read_feeds()
        self.assertEqual({r["name"]: r["state"] for r in rows}, expected)
        for row in rows:
            self.assertEqual(row["result"], int(row["state"] == "Failed"))
            self.assertEqual(row["source"], "feed")
            self.assertEqual(row["progress"], 0.4)

    def test_stale_running_feed_stays_with_no_update_message(self):
        self.seed("boundary", "running", 600)
        self.seed("silent", "running", 601)
        self.seed("ancient", "running", 90000)
        with patch.object(self.server.time, "time", return_value=2_000_000):
            rows = {r["name"]: r for r in self.server.read_feeds()}
        self.assertEqual(rows["boundary"]["log"], "step")
        self.assertIn("no update for 10 min", rows["silent"]["log"])
        self.assertIn("ancient", rows)

    def test_finished_expiry_and_newest_first_sort(self):
        self.seed("old", "done", 86401)
        self.seed("boundary", "done", 86400)
        self.seed("middle", "failed", 10)
        self.seed("new", "running", 1)
        with patch.object(self.server.time, "time", return_value=2_000_000):
            rows = self.server.read_feeds()
        self.assertEqual([r["name"] for r in rows], ["new", "middle", "boundary"])

    def test_corrupt_json_is_ignored(self):
        path = Path(self.server.FEEDS_DIR)
        path.mkdir()
        (path / "partial.json").write_text("{")
        self.assertEqual(self.server.read_feeds(), [])

