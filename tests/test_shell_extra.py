"""More shell.py checks via AST extraction (importing shell would load webview and Win32 UI)."""
import ast
import time
import unittest

from support import ROOT


class ShellExtraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "shell.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wanted_funcs = {"parse_hotkey", "tucked_y", "log"}
        wanted_assign = {"MOD_ALT", "MOD_CONTROL", "MOD_SHIFT", "MOD_WIN", "MOD_NOREPEAT", "PEEK", "HERE", "LOG"}
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
                nodes.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "Api":
                nodes.append(node)
            elif isinstance(node, ast.Assign):
                names = {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
                if names and names <= wanted_assign:
                    nodes.append(node)
        import os, sys, time, threading
        cls.namespace = {"os": os, "sys": sys, "time": time, "threading": threading, "__file__": str(path)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), cls.namespace)

    def setUp(self):
        self.ns = self.namespace
        self.ns["S"] = {"pinned": False, "away_since": 5, "quit": False}
        self.ns["SH"] = {"pill_width": 180}

    def test_hotkey_function_keys_and_modifiers(self):
        parse = self.ns["parse_hotkey"]
        self.assertEqual(parse("f1"), (0x4000, 0x70))
        self.assertEqual(parse("ctrl+shift+f24"), (0x4006, 0x6F + 24))
        self.assertEqual(parse("alt+9"), (0x4001, ord("9")))

    def test_hotkey_without_main_key_or_junk(self):
        parse = self.ns["parse_hotkey"]
        self.assertEqual(parse("ctrl+alt"), (0x4003, 0))
        self.assertEqual(parse(""), (0x4000, 0))
        self.assertEqual(parse("ctrl+bogus+x"), (0x4002, ord("X")))
        self.assertEqual(parse("ctrl+fx"), (0x4002, 0))

    def test_hotkey_last_key_wins(self):
        self.assertEqual(self.ns["parse_hotkey"]("a+b")[1], ord("B"))

    def test_tucked_y_scale_one(self):
        self.ns["S"] = {"top": 0, "h": 740, "scale": 1.0}
        self.assertEqual(self.ns["tucked_y"](), -730)

    def test_api_pin_sets_and_clears_away_timer(self):
        api = self.ns["Api"]()
        self.assertIs(api.pin(True), True)
        self.assertTrue(self.ns["S"]["pinned"])
        self.assertIsNone(self.ns["S"]["away_since"])
        self.assertIs(api.pin(0), False)
        self.assertFalse(self.ns["S"]["pinned"])

    def test_api_state_and_config(self):
        api = self.ns["Api"]()
        self.assertTrue(api.state("anything"))
        self.assertEqual(api.config(), {"pill_width": 180})

    def test_api_quit_sets_flag_and_destroys_window(self):
        destroyed = []

        class Window:
            def destroy(self):
                destroyed.append(1)

        self.ns["S"]["window"] = Window()
        self.ns["Api"]().quit()
        for _ in range(200):
            if destroyed:
                break
            time.sleep(0.01)
        self.assertTrue(self.ns["S"]["quit"])
        self.assertEqual(destroyed, [1])

    def test_log_appends_and_swallows_errors(self):
        import tempfile, os
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as d:
            target = os.path.join(d, "shell.log")
            self.ns["LOG"] = target
            self.ns["log"]("first")
            self.ns["log"]("second")
            with open(target, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual([l[20:] for l in lines], ["first", "second"])
            self.ns["LOG"] = os.path.join(d, "missing", "x.log")
            self.ns["log"]("ignored")        # must not raise
