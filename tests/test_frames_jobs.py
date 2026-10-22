import json
import os
from pathlib import Path
from unittest.mock import patch

from support import IsolatedTest


class FrameTests(IsolatedTest):
    def test_render_command_lines(self):
        # Real Windows paths, including spaces and a positional ffmpeg output.
        for name, args in [
            ("blender.exe", '-b scene.blend -o "{folder}\\f_####" -a'),
            ("blender.exe", '-b scene.blend -o "{folder}\\c_####" -a'),
            ("python.exe", 'render.py --out "{folder}" --count 10'),
            ("pwsh.exe", '-File render.ps1 --render "{folder}"'),
            ("ffmpeg.exe", '-i "C:\\input\\movie.mp4" "{folder}\\frame_%04d.png"'),
        ]:
            for dirname in ("frames", "frames with spaces"):
                with self.subTest(name=name, args=args, dirname=dirname):
                    folder = self.home / dirname
                    folder.mkdir(exist_ok=True)
                    rows = [{"Name": name, "CommandLine": name + " " + args.format(folder=folder)}]
                    self.assertEqual(self.server.render_dirs(rows), [str(folder)])

    def test_unquoted_output_and_unsupported_process(self):
        # A synthetic path keeps this case unquoted even in a checkout with spaces.
        folder = r"D:\renders\frames"
        command = f"render.py --out {folder} --count 2"
        with patch.object(self.server.os.path, "isdir", side_effect=lambda p: p == folder):
            self.assertEqual(self.server.render_dirs([{"Name": "python.exe", "CommandLine": command}]), [folder])
            self.assertEqual(self.server.render_dirs([{"Name": "notepad.exe", "CommandLine": command}]), [])

    def test_ffmpeg_input_is_not_an_output_directory(self):
        image = self.home / "input.png"
        image.write_bytes(b"image fixture")
        rows = [{"Name": "ffmpeg.exe", "CommandLine": f'ffmpeg.exe -i "{image}"'}]
        self.assertEqual(self.server.render_dirs(rows), [])

    def test_extra_globs_age_filter_and_any_age(self):
        recent, old = self.home / "frames-new", self.home / "frames-old"
        recent.mkdir()
        old.mkdir()
        os.utime(recent, (1999990, 1999990))
        os.utime(old, (1999000, 1999000))
        self.server.CFG["extra_frame_globs"] = [str(self.home / "frames-*")]
        with patch.object(self.server.time, "time", return_value=2_000_000):
            self.assertEqual(self.server.render_dirs([]), [str(recent)])
            self.assertEqual(self.server.render_dirs([], any_age=True), sorted([str(recent), str(old)]))

    def test_feed_frames_directories(self):
        active, done = self.home / "active", self.home / "done"
        active.mkdir()
        done.mkdir()
        self.server.write_feed({"name": "active", "frames_dir": str(active)})
        self.server.write_feed({"name": "done", "state": "done", "frames_dir": str(done)})
        self.assertEqual(self.server.render_dirs([]), [str(active)])
        self.assertEqual(self.server.render_dirs([], any_age=True), [str(active), str(done)])

    def test_recent_frames_filters_sorts_and_limits(self):
        for index, name in enumerate(("old.png", "middle.JPG", "new.jpeg", "ignored.exr", "notes.txt")):
            path = self.home / name
            path.write_bytes(b"image fixture")
            os.utime(path, (1000 + index, 1000 + index))
        (self.home / "directory.png").mkdir()
        self.server.FRAME["dir"] = str(self.home)
        self.assertEqual(self.server.recent_frames(2), [{"name": "new.jpeg", "mtime": 1002}, {"name": "middle.JPG", "mtime": 1001}])
        self.assertEqual(len(self.server.recent_frames()), 3)

    def test_recent_frames_without_directory(self):
        self.assertEqual(self.server.recent_frames(), [])
        self.server.FRAME["dir"] = str(self.home / "missing")
        self.assertEqual(self.server.recent_frames(), [])


class LogAndJobsTests(IsolatedTest):
    def test_log_variables_d_and_w(self):
        for variable in ("d", "w"):
            with self.subTest(variable=variable):
                log = self.home / "render.log"
                log.write_text("first\nlast useful line\n\n", encoding="utf-8")
                script = self.home / "render.ps1"
                script.write_text(f'${variable} = "{self.home}"\n$log = "${variable}\\render.log"', encoding="utf-8")
                self.assertEqual(self.server.last_log_line(f'-NoProfile -File "{script}"'), "last useful line")

    def test_quoted_script_path_with_spaces(self):
        script = self.home / "render job.ps1"
        log = self.home / "job.log"
        log.write_text("completed\n", encoding="utf-8")
        script.write_text(f'$log = "{log}"', encoding="utf-8")
        self.assertEqual(self.server.last_log_line(f'-NoProfile -File "{script}" -Verbose'), "completed")

    def test_missing_script_or_log_returns_empty(self):
        self.assertEqual(self.server.last_log_line("-Command foo"), "")
        self.assertEqual(self.server.last_log_line(f"-File {self.home / 'missing.ps1'}"), "")
        script = self.home / "render.ps1"
        script.write_text('$log = "missing.log"')
        self.assertEqual(self.server.last_log_line(f"-File {script}"), "")

    def test_jobs_parse_sort_and_never_run(self):
        self.server.CFG["task_folder"] = "\\renders\\"
        rows = [
            {"name": "never", "state": "Ready", "last": "", "result": 267011, "action": ""},
            {"name": "old", "state": "Ready", "last": "2026-10-20 10:00", "result": 0},
            {"name": "new", "state": "Ready", "last": "2026-10-22 10:00", "result": 0},
            {"name": "active", "state": "Running", "last": "2026-10-19 10:00", "result": 267009},
            {"name": "queued", "state": "Queued", "last": "", "result": 0},
            {"name": "disabled", "state": "Disabled", "last": "", "result": 0},
        ]
        with patch.object(self.server, "ps", return_value=json.dumps(rows)) as ps:
            result = self.server.jobs(0)
        self.assertEqual([r["name"] for r in result], ["active", "queued", "new", "old", "never", "disabled"])
        self.assertEqual(result[4]["last"], "")
        self.assertEqual(result[4]["result"], 267011)
        self.assertEqual(result[4]["log"], "")
        self.assertIn("$t.Year -gt 2000", ps.call_args.args[0])

    def test_jobs_single_object_and_log(self):
        self.server.CFG["task_folder"] = "renders"
        script, log = self.home / "job.ps1", self.home / "job.log"
        log.write_text("frame 42\n")
        script.write_text(f'$log = "{log}"')
        row = {"name": "one", "state": "Running", "last": "", "action": f'-File "{script}"'}
        with patch.object(self.server, "ps", return_value=json.dumps(row)):
            result = self.server.jobs(0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["log"], "frame 42")

    def test_jobs_disabled_folder_does_not_query_powershell(self):
        self.assertEqual(self.server.jobs(0), [])
        self.server.ps.assert_not_called()

