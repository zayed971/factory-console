import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from unittest.mock import Mock

from support import IsolatedTest, load_module


def rows():
    return [
        {"ProcessId": 10, "Name": "blender.exe", "CommandLine": r"blender D:\jobs\a.blend", "ParentProcessId": 1},
        {"ProcessId": 11, "Name": "ffmpeg.exe", "CommandLine": r"ffmpeg D:\jobs\b.mp4", "ParentProcessId": 1},
    ]


class StateTests(IsolatedTest):
    def test_missing_state_gives_defaults(self):
        st = self.server._state()
        self.assertFalse(st["game_mode"])
        self.assertEqual((st["suspended"], st["restart_tasks"], st["log"]), ([], [], []))
        self.assertTrue(st["auto"])

    def test_corrupt_state_gives_defaults(self):
        Path(self.server.STATE_FILE).write_text("{not json", encoding="utf-8")
        self.assertFalse(self.server._state()["game_mode"])

    def test_save_roundtrip_and_log_trim(self):
        st = self.server._state()
        st["log"] = [str(i) for i in range(100)]
        self.server._save(st)
        loaded = self.server._state()
        self.assertEqual(loaded["log"], [str(i) for i in range(60, 100)])

    def test_save_writes_flag_file(self):
        flag = self.home / "flag.json"
        st = self.server._state()
        st.update(game_mode=True, since="now", reason="manual")
        with patch.object(self.server, "FLAG_FILE", str(flag)):
            self.server._save(st)
        self.assertEqual(json.loads(flag.read_text()), {"game_mode": True, "since": "now", "reason": "manual"})

    def test_note_appends_timestamped_line(self):
        st = {}
        self.server._note(st, "hello")
        self.assertEqual(len(st["log"]), 1)
        self.assertTrue(st["log"][0].endswith(" hello"))


class GameModeTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        # IsolatedTest forbids game_on/game_off/free_gpu/_sr; load a copy that keeps the real ones, with process access still blocked.
        self.server = load_module("game_server", self.home / "server.py")
        self.server.k32 = Mock()
        self.server.ntdll = Mock()
        for name in ("ps", "run", "_sr"):
            self.enterContext(patch.object(self.server, name, side_effect=AssertionError(name + " forbidden")))
        self.server.CFG.update(factory_paths=[r"D:\jobs"], never_pause=[], stop_ollama_in_game=False)

    def test_game_on_freezes_matching_processes_only(self):
        frozen = []
        def sr(pid, fn):
            frozen.append(pid)
            return pid != 11          # 11 refuses to suspend
        with patch.object(self.server, "processes", return_value=rows()), patch.object(self.server, "_sr", side_effect=sr):
            st = self.server.game_on("manual")
        self.assertEqual(frozen, [10, 11])
        self.assertTrue(st["game_mode"])
        self.assertEqual([p["pid"] for p in st["suspended"]], [10])
        self.assertEqual(st["reason"], "manual")
        self.assertTrue(self.server._state()["game_mode"])
        self.assertIn("froze 1 work processes", st["log"][-1])

    def test_game_on_twice_is_a_noop(self):
        with patch.object(self.server, "processes", return_value=rows()), patch.object(self.server, "_sr", return_value=True) as sr:
            self.server.game_on("manual")
            calls = sr.call_count
            st = self.server.game_on("again")
        self.assertEqual(sr.call_count, calls)
        self.assertEqual(st["reason"], "manual")

    def test_game_off_resumes_and_resets(self):
        with patch.object(self.server, "processes", return_value=rows()), patch.object(self.server, "_sr", return_value=True):
            self.server.game_on("manual")
        st = self.server._state()
        st["restart_tasks"] = ["Render's"]
        self.server._save(st)
        self.server.CFG["task_folder"] = "\\renders\\"
        resumed, scripts = [], []
        def sr(pid, fn):
            resumed.append(pid)
            return True
        with patch.object(self.server, "_sr", side_effect=sr), patch.object(self.server, "ps", side_effect=lambda s, t=30: scripts.append(s)):
            st = self.server.game_off("manual")
        self.assertEqual(resumed, [10, 11])
        self.assertEqual(scripts, ["Start-ScheduledTask -TaskPath '\\renders\\' -TaskName 'Renders'"])
        self.assertFalse(st["game_mode"])
        self.assertEqual((st["suspended"], st["restart_tasks"], st["since"], st["reason"]), ([], [], None, None))
        self.assertIn("resumed 2 processes, restarted 1 tasks", self.server._state()["log"][-1])

    def test_game_off_when_not_on_is_a_noop(self):
        with patch.object(self.server, "_sr") as sr:
            st = self.server.game_off("manual")
        sr.assert_not_called()
        self.assertFalse(st["game_mode"])

    def test_sweep_freezes_only_new_processes(self):
        self.assertEqual(self.server.game_sweep(), 0)         # game mode off
        with patch.object(self.server, "processes", return_value=rows()[:1]), patch.object(self.server, "_sr", return_value=True):
            self.server.game_on("manual")
        frozen = []
        def sr(pid, fn):
            frozen.append(pid)
            return True
        with patch.object(self.server, "processes", return_value=rows()), patch.object(self.server, "_sr", side_effect=sr):
            self.assertEqual(self.server.game_sweep(), 1)
            self.assertEqual(self.server.game_sweep(), 0)
        self.assertEqual(frozen, [11])
        self.assertEqual([p["pid"] for p in self.server._state()["suspended"]], [10, 11])

    def test_free_gpu_stops_only_heavy_blender_and_queues_task(self):
        st = self.server._state()
        st.update(game_mode=True, suspended=[
            {"pid": 10, "name": "Blender.exe", "cmd": ""}, {"pid": 11, "name": "ffmpeg.exe", "cmd": ""},
            {"pid": 12, "name": "blender.exe", "cmd": ""}])
        self.server._save(st)
        self.server.JOBS_CACHE["rows"] = [{"name": "Nightly", "action": r"pwsh -File D:\jobs\render.ps1"}]
        procs = [{"ProcessId": 10, "Name": "blender.exe", "CommandLine": "blender", "ParentProcessId": 5},
                 {"ProcessId": 5, "Name": "pwsh.exe", "CommandLine": r"pwsh -File D:\JOBS\render.ps1", "ParentProcessId": 1}]
        with patch.object(self.server, "gpu_apps", return_value={10: 900, 11: 900, 12: 100}), \
                patch.object(self.server, "processes", return_value=procs), \
                patch.object(self.server.subprocess, "run") as kill:
            st = self.server.free_gpu()
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[0], ["taskkill", "/PID", "10", "/T", "/F"])
        self.assertEqual([p["pid"] for p in st["suspended"]], [11, 12])
        self.assertEqual(st["restart_tasks"], ["Nightly"])

    def test_task_of_walks_parents_and_gives_up(self):
        self.server.JOBS_CACHE["rows"] = [{"name": "T", "action": r"-File c:\x\run.ps1"}]
        by_pid = {1: {"CommandLine": "blender", "ParentProcessId": 2},
                  2: {"CommandLine": r"pwsh -File C:\X\RUN.ps1", "ParentProcessId": 3}}
        self.assertEqual(self.server.task_of(1, by_pid), "T")
        self.assertIsNone(self.server.task_of(99, by_pid))
        self.assertIsNone(self.server.task_of(1, {1: {"CommandLine": "x", "ParentProcessId": 1}}))


class LedgerInfoTests(IsolatedTest):
    def make_db(self):
        path = self.home / "ledger.db"
        c = sqlite3.connect(path)
        c.execute("create table tasks(id text, title text, status text, updated text)")
        c.execute("create table events(ts text, actor text, task text, msg text)")
        c.executemany("insert into tasks values(?,?,?,?)", [("a", "A" * 200, "needs-you", "2"), ("b", "B", "needs-you", "3"), ("c", "C", "done", "1")])
        c.executemany("insert into events values(?,?,?,?)", [("2026-10-01 10:00", "bot", "a", "m" * 300), ("2026-10-01 11:00", "factory_watch", "a", "x"), ("2026-10-01 12:00", "me", "b", None)])
        c.commit(); c.close()
        return path

    def test_ledger_off_when_unset_or_missing(self):
        self.assertEqual(self.server.ledger(), {"off": True})
        self.server.CFG["ledger"] = str(self.home / "nope.db")
        self.assertEqual(self.server.ledger(), {"off": True})

    def test_ledger_summarises_tasks_and_events(self):
        self.server.CFG["ledger"] = str(self.make_db())
        out = self.server.ledger()
        self.assertEqual(out["counts"], {"needs-you": 2, "done": 1})
        self.assertEqual([t["id"] for t in out["you"]], ["b", "a"])
        self.assertEqual(len(out["you"][1]["t"]), 160)
        self.assertEqual([e["who"] for e in out["events"]], ["me", "bot"])
        self.assertEqual(out["events"][0]["msg"], "")
        self.assertEqual(out["events"][1]["ts"], "10-01 10:00")
        self.assertEqual(len(out["events"][1]["msg"]), 200)

    def test_ledger_bad_database_reports_error(self):
        bad = self.home / "bad.db"
        bad.write_bytes(b"not a database at all")
        self.server.CFG["ledger"] = str(bad)
        self.assertIn("error", self.server.ledger())

    def test_info_features(self):
        self.server.CFG.update(task_folder="", ledger="")
        self.server.SNAP["data"] = {"gpu": None}
        info = self.server.info()
        self.assertEqual(info["features"], {"jobs": False, "ledger": False, "gpu": False})
        self.assertEqual(info["version"], self.server.VERSION)
        self.server.CFG["task_folder"] = "\\r\\"
        self.server.SNAP["data"] = None
        info = self.server.info()
        self.assertTrue(info["features"]["jobs"])
        self.assertTrue(info["features"]["gpu"])

    def test_snapshot_refreshes_frame_age(self):
        self.server.SNAP["data"] = {"frame": {"age_s": 999}, "x": 1}
        self.server.FRAME["mtime"] = self.server.time.time() - 50
        d = self.server.snapshot()
        self.assertAlmostEqual(d["frame"]["age_s"], 50, delta=3)
        self.assertEqual(self.server.SNAP["data"]["frame"]["age_s"], 999)
