import http.client
import json
import os
from pathlib import Path
import socket
from unittest.mock import patch
from urllib.parse import quote
import urllib.request

from support import IsolatedTest, ROOT, load_module


class HTTPTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.start_server()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type")
        finally:
            connection.close()

    def test_post_feed_persists_and_returns_normalized_row(self):
        status, body, _ = self.request("POST", "/api/feed", json.dumps({"name": "HTTP", "state": "RUNNING", "progress": 2}))
        self.assertEqual(status, 200)
        row = json.loads(body)
        self.assertEqual(row["progress"], 1)
        self.assertEqual(row["state"], "running")
        self.assertEqual(json.loads((Path(self.server.FEEDS_DIR) / "HTTP.json").read_text()), row)

    def test_bad_json_and_missing_name_return_400(self):
        for body in (b"{", b"{}", b"[]"):
            with self.subTest(body=body):
                status, body, _ = self.request("POST", "/api/feed", body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        self.assertFalse(Path(self.server.FEEDS_DIR).exists())

    def test_over_64kb_returns_413(self):
        status, _, _ = self.request("POST", "/api/feed", b"x" * 65537)
        self.assertEqual(status, 413)
        self.assertFalse(Path(self.server.FEEDS_DIR).exists())

    def test_exactly_64kb_is_accepted(self):
        body = b'{"name":"limit"}'
        status, _, _ = self.request("POST", "/api/feed", body + b" " * (65536 - len(body)))
        self.assertEqual(status, 200)

    def test_foreign_origin_returns_403(self):
        for origin in ("https://foreign.example", self.url + ".evil.example", "null"):
            with self.subTest(origin=origin):
                status, _, _ = self.request("POST", "/api/feed", b'{"name":"blocked"}', {"Origin": origin})
                self.assertEqual(status, 403)
        self.assertFalse(Path(self.server.FEEDS_DIR).exists())

    def test_local_origins_are_accepted(self):
        for origin in (self.url, f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                status, _, _ = self.request("POST", "/api/feed", b'{"name":"local"}', {"Origin": origin})
                self.assertEqual(status, 200)

    def test_second_serve_returns_false(self):
        # The actual bind must fail; background watchers must not start twice.
        with patch.object(self.server, "_save") as save:
            self.assertIs(self.server.serve(port=self.port, host="127.0.0.1"), False)
        save.assert_not_called()
        for name in ("auto_loop", "sampler_loop", "job_loop"):
            getattr(self.server, name).assert_called_once()
        self.assertEqual(self.request("GET", "/api/info")[0], 200)

    def test_frame_guard_and_valid_image(self):
        folder = self.home / "frames"
        folder.mkdir()
        image = b"test image bytes"
        (folder / "visible.PNG").write_bytes(image)
        (folder / "notes.txt").write_text("private")
        secret = self.home / "secret.png"
        secret.write_bytes(b"secret")
        self.server.FRAME.update(dir=str(folder), path=str(folder / "visible.PNG"))
        for name in (r"..\secret.png", "../secret.png", str(secret), str(folder / "visible.PNG"), "notes.txt", "missing.png"):
            with self.subTest(name=name):
                status, body, _ = self.request("GET", "/frame?name=" + quote(name, safe=""))
                self.assertEqual(status, 404)
                self.assertNotIn(b"secret", body)
        status, body, content_type = self.request("GET", "/frame?name=visible.PNG")
        self.assertEqual((status, body, content_type), (200, image, "image/png"))
        self.assertEqual(self.request("GET", "/frame")[1], image)

    def test_report_over_http(self):
        fallback = self.home / "fallback"
        with patch.dict(os.environ, {"FACTORY_URL": self.url, "FACTORY_FEEDS": str(fallback)}):
            reporter = load_module("reporter_http", ROOT / "factory_feed.py")
        # Bypass environment proxy settings; only the test's loopback server is used.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch.object(reporter.urllib.request, "urlopen", opener.open):
            self.assertTrue(reporter.report("reporter", progress=0.5, log="half", frames_dir=str(self.home)))
        row = self.server.read_feeds()[0]
        self.assertEqual((row["name"], row["progress"], row["log"]), ("reporter", 0.5, "half"))
        self.assertEqual(row["frames_dir"], str(self.home))
        self.assertFalse(fallback.exists())


class ReporterTests(IsolatedTest):
    def test_report_falls_back_when_port_not_listening(self):
        # Keep a socket bound but not listening: no other process can steal the port.
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{closed.getsockname()[1]}"
            feeds = self.home / "fallback"
            with patch.dict(os.environ, {"FACTORY_URL": url, "FACTORY_FEEDS": str(feeds)}):
                reporter = load_module("reporter_fallback", ROOT / "factory_feed.py")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with patch.object(reporter.urllib.request, "urlopen", opener.open):
                self.assertFalse(reporter.report("offline job", "done", 1, "saved", str(self.home)))
        files = list(feeds.iterdir())
        self.assertEqual([p.name for p in files], ["offline_job.json"])
        row = json.loads(files[0].read_text())
        self.assertEqual(row["name"], "offline job")
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["progress"], 1)
        self.assertEqual(row["log"], "saved")
        self.assertEqual(row["frames_dir"], str(self.home))
        self.assertIsInstance(row["updated"], float)

    def test_feed_throttling_and_force(self):
        reporter = load_module("reporter_logic", ROOT / "factory_feed.py")
        with patch.object(reporter, "report") as report, patch.object(reporter.time, "time", side_effect=[100, 100.5, 100.6, 101.7]):
            feed = reporter.Feed("job")
            feed.update(0.1)
            feed.update(0.2)
            feed.update(0.3, force=True)
            feed.update(0.4)
        self.assertEqual([c.args[2] for c in report.call_args_list], [None, 0.1, 0.3, 0.4])

    def test_feed_context_success_and_failure(self):
        reporter = load_module("reporter_context", ROOT / "factory_feed.py")
        with patch.object(reporter, "report") as report:
            with reporter.Feed("success"):
                pass
            report.assert_called_with("success", "done", 1.0, "", "")
            with self.assertRaisesRegex(ValueError, "broken"):
                with reporter.Feed("failure"):
                    raise ValueError("broken")
            report.assert_called_with("failure", "failed", None, "ValueError: broken", "")

