import http.client
import json
from pathlib import Path
from unittest.mock import patch

from support import IsolatedTest


class RouteTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        static = self.home / "static"
        static.mkdir()
        (static / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
        (static / "app.css").write_text("body{}", encoding="utf-8")
        (static / "logo.svg").write_text("<svg/>", encoding="utf-8")
        (static / "data.bin").write_bytes(b"\x00\x01")
        (self.home / "secret.txt").write_text("secret", encoding="utf-8")
        self.start_server()

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            c.request(method, path, body, headers or {})
            r = c.getresponse()
            return r.status, r.read(), r.getheader("Content-Type"), r.getheader("Cache-Control")
        finally:
            c.close()

    def test_index_served_at_root_and_index_html(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, body, ctype, cache = self.request("GET", path)
                self.assertEqual((status, body, ctype, cache), (200, b"<h1>home</h1>", "text/html; charset=utf-8", "no-store"))

    def test_static_content_types(self):
        for name, ctype in (("app.css", "text/css; charset=utf-8"), ("logo.svg", "image/svg+xml"), ("data.bin", "application/octet-stream")):
            with self.subTest(name=name):
                status, _, got, _ = self.request("GET", "/static/" + name)
                self.assertEqual((status, got), (200, ctype))

    def test_static_missing_and_traversal_are_404(self):
        for path in ("/static/nope.css", "/static/../secret.txt", "/static/%2e%2e/secret.txt", "/static/..%5csecret.txt", "/static/"):
            with self.subTest(path=path):
                status, body, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"secret", body)

    def test_unknown_get_and_post_are_404(self):
        self.assertEqual(self.request("GET", "/nothing")[:2], (404, b"{}"))
        self.assertEqual(self.request("POST", "/nothing")[:2], (404, b"{}"))

    def test_api_info_json(self):
        status, body, ctype, _ = self.request("GET", "/api/info")
        data = json.loads(body)
        self.assertEqual((status, ctype), (200, "application/json; charset=utf-8"))
        self.assertEqual(data["version"], self.server.VERSION)
        self.assertEqual(data["games"], self.server.CFG["games"])
        self.assertIn("features", data)

    def test_api_snapshot_uses_cached_sample(self):
        self.server.SNAP["data"] = {"cpu": 12.5, "frame": None}
        self.assertEqual(json.loads(self.request("GET", "/api/snapshot")[1]), {"cpu": 12.5, "frame": None})

    def test_api_ledger_off(self):
        self.assertEqual(json.loads(self.request("GET", "/api/ledger")[1]), {"off": True})

    def test_api_jobs_falls_back_to_processes_and_orders_feeds(self):
        self.server.SNAP["data"] = {"factory_procs": [{"name": "blender.exe", "pid": 7, "cmd": "blender x"}]}
        self.server.CFG["task_folder"] = ""
        self.server.write_feed({"name": "running one", "state": "running"})
        self.server.write_feed({"name": "finished one", "state": "done"})
        jobs = json.loads(self.request("GET", "/api/jobs")[1])
        self.assertEqual([j["name"] for j in jobs], ["running one", "blender.exe 7", "finished one"])
        self.assertEqual(jobs[1]["source"], "process")

    def test_api_jobs_uses_task_scheduler_when_folder_set(self):
        self.server.CFG["task_folder"] = "\\r\\"
        row = {"name": "T", "state": "Ready", "last": "", "log": ""}
        with patch.object(self.server, "jobs", return_value=[row]):
            self.assertEqual(json.loads(self.request("GET", "/api/jobs")[1]), [row])

    def test_api_frames_lists_recent_frames(self):
        folder = self.home / "frames"
        folder.mkdir()
        for n in ("a.png", "b.png", "c.png"):
            (folder / n).write_bytes(b"x")
        self.server.FRAME.update(dir=str(folder))
        data = json.loads(self.request("GET", "/api/frames?n=2")[1])
        self.assertEqual(data["dir"], str(folder))
        self.assertEqual(len(data["frames"]), 2)

    def test_frame_without_current_render_is_404(self):
        self.server.FRAME.update(path=None, dir=None)
        self.assertEqual(self.request("GET", "/frame")[0], 404)
        self.assertEqual(self.request("GET", "/frame?name=a.png")[0], 404)

    def test_jpeg_frame_content_type(self):
        folder = self.home / "frames"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"jpg")
        self.server.FRAME.update(dir=str(folder), path=str(folder / "a.jpg"))
        self.assertEqual(self.request("GET", "/frame?name=a.jpg")[::2][0], 200)
        self.assertEqual(self.request("GET", "/frame?name=a.jpg")[2], "image/jpeg")

    def test_game_on_off_routes(self):
        with patch.object(self.server, "game_on", return_value={"game_mode": True}) as on, \
                patch.object(self.server, "game_off", return_value={"game_mode": False}) as off, \
                patch.object(self.server, "free_gpu", return_value={"freed": 1}) as free:
            self.assertEqual(json.loads(self.request("POST", "/api/game/on")[1]), {"game_mode": True})
            self.assertEqual(json.loads(self.request("POST", "/api/game/off")[1]), {"game_mode": False})
            self.assertEqual(json.loads(self.request("POST", "/api/game/free-gpu")[1]), {"freed": 1})
        on.assert_called_once_with("manual")
        off.assert_called_once_with("manual")
        free.assert_called_once_with()

    def test_game_routes_reject_foreign_origin(self):
        with patch.object(self.server, "game_on") as on:
            status = self.request("POST", "/api/game/on", headers={"Origin": "https://evil.example"})[0]
        self.assertEqual(status, 403)
        on.assert_not_called()

    def test_game_auto_toggle_persists(self):
        st = json.loads(self.request("POST", "/api/game/auto?on=0")[1])
        self.assertFalse(st["auto"])
        self.assertFalse(self.server._state()["auto"])
        self.assertIn("auto game mode off", st["log"][-1])
        st = json.loads(self.request("POST", "/api/game/auto?on=1")[1])
        self.assertTrue(st["auto"])
        self.assertTrue(self.server._state()["auto"])
        st = json.loads(self.request("POST", "/api/game/auto")[1])        # default is on
        self.assertTrue(st["auto"])

    def test_feed_invalid_content_length_is_400(self):
        status, body, _, _ = self.request("POST", "/api/feed", b"", {"Content-Length": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
