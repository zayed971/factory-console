"""Isolated fixtures: no live console, process commands, or external network."""
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        # Inherit the workspace ACL: Python 3.13's tempfile mode 0700 can exclude
        # the restricted Windows sandbox token from its own new directory.
        self.home = ROOT / "tests" / ("fixture-" + uuid.uuid4().hex)
        self.home.mkdir()
        self.addCleanup(shutil.rmtree, self.home)
        for name in ("run", "Popen"):
            self.enterContext(patch.object(subprocess, name, side_effect=AssertionError("process command forbidden")))
        shutil.copyfile(ROOT / "server.py", self.home / "server.py")
        self.server = load_module("isolated_server", self.home / "server.py")
        self.server.k32 = Mock(spec=[])
        self.server.ntdll = Mock(spec=[])
        for name in ("ps", "run", "_sr", "game_on", "game_off", "free_gpu"):
            self.enterContext(patch.object(self.server, name, side_effect=AssertionError(name + " forbidden")))

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def start_server(self):
        server = self.server
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.write_json(Path(server.CONFIG_FILE), {"host": "127.0.0.1", "port": port})
        server.CFG = server.load_config()
        server.PORT = port
        for name in ("auto_loop", "sampler_loop", "job_loop"):
            self.enterContext(patch.object(server, name, return_value=None))
        ready = threading.Event()
        instances, errors = [], []

        class CapturedServer(server.ThreadingHTTPServer):
            def serve_forever(self, poll_interval=0.01):
                instances.append(self)
                ready.set()
                return super().serve_forever(poll_interval)

        self.enterContext(patch.object(server, "ThreadingHTTPServer", CapturedServer))

        def run():
            try:
                server.serve()
            except BaseException as exc:
                errors.append(exc)
                ready.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def stop():
            if instances:
                instances[0].shutdown()
                instances[0].server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive(), "test HTTP server did not stop")
            self.assertEqual(errors, [])

        self.addCleanup(stop)
        self.assertTrue(ready.wait(3), "test HTTP server did not start")
        self.assertEqual(errors, [])
        self.port = port
        self.url = f"http://127.0.0.1:{port}"

