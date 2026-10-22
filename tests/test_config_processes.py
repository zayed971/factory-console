import json
from pathlib import Path
from unittest.mock import patch

from support import IsolatedTest


class ConfigTests(IsolatedTest):
    def test_missing_config_is_created(self):
        path = self.home / "new.json"
        with patch.object(self.server, "CONFIG_FILE", str(path)):
            cfg = self.server.load_config()
        self.assertEqual(cfg, self.server.DEFAULTS)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), cfg)

    def test_defaults_merge_without_mutation(self):
        self.write_json(Path(self.server.CONFIG_FILE), {"title": "Test", "games": ["test.exe"]})
        cfg = self.server.load_config()
        self.assertEqual(cfg["title"], "Test")
        self.assertEqual(cfg["port"], 8787)
        self.assertEqual(cfg["games"], ["test.exe"])
        cfg["pausable_names"].append("test.exe")
        self.assertNotIn("test.exe", self.server.DEFAULTS["pausable_names"])

    def test_bom_config_loads(self):
        Path(self.server.CONFIG_FILE).write_text('{"title": "BOM"}', encoding="utf-8-sig")
        self.assertEqual(self.server.load_config()["title"], "BOM")

    def test_shell_subkeys_merge(self):
        self.write_json(Path(self.server.CONFIG_FILE), {"shell": {"width": 900}})
        shell = self.server.load_config()["shell"]
        self.assertEqual(shell, {**self.server.DEFAULTS["shell"], "width": 900})
        self.assertEqual(self.server.DEFAULTS["shell"]["width"], 1240)

    def test_null_shell_uses_defaults(self):
        self.write_json(Path(self.server.CONFIG_FILE), {"shell": None})
        self.assertEqual(self.server.load_config()["shell"], self.server.DEFAULTS["shell"])


class ProcessTests(IsolatedTest):
    def test_names_paths_and_never_pause(self):
        self.server.CFG.update(factory_paths=[r"D:\jobs"], never_pause=["console"])
        rows = [
            {"Name": "BLENDER.EXE", "CommandLine": r"blender D:\JOBS\scene.blend"},
            {"Name": "ffmpeg.exe", "CommandLine": r"ffmpeg D:\jobs\clip.mp4"},
            {"Name": "python.exe", "CommandLine": r"python D:\jobs\run.py"},
            {"Name": "blender.exe", "CommandLine": r"blender D:\personal\scene.blend"},
            {"Name": "blender.exe", "CommandLine": r"blender D:\jobs\CONSOLE\scene.blend"},
            {"Name": "blender.exe", "CommandLine": None},
            {"Name": None, "CommandLine": ""},
        ]
        self.assertEqual(self.server.factory_procs(rows), rows[:2])

    def test_empty_factory_paths_match_any_work_path(self):
        self.server.CFG.update(factory_paths=[], never_pause=["factory_console"])
        rows = [{"Name": "ffmpeg.exe", "CommandLine": r"ffmpeg C:\personal\clip.mp4"},
                {"Name": "ffmpeg.exe", "CommandLine": "ffmpeg factory_console"}]
        self.assertEqual(self.server.factory_procs(rows), rows[:1])

    def test_games_are_case_insensitive_sorted_and_unique(self):
        self.server.CFG["games"] = ["cs2.exe", "eldenring.exe"]
        rows = [{"Name": n} for n in ["eldenring.exe", "CS2.EXE", "CS2.EXE", "notcs2.exe", None]]
        self.assertEqual(self.server.games_running(rows), ["CS2.EXE", "eldenring.exe"])

    def test_gpu_tools_absent(self):
        with patch.object(self.server, "run", return_value=""):
            self.assertIsNone(self.server.gpu())
            self.assertEqual(self.server.gpu_apps(), {})

