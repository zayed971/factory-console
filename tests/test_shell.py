"""Compile only pure AST nodes: importing shell would import webview and Win32 UI."""
import ast
import unittest

from support import ROOT


class ShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "shell.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {"parse_hotkey", "tucked_y"}
        constants = {"MOD_ALT", "MOD_CONTROL", "MOD_SHIFT", "MOD_WIN", "MOD_NOREPEAT", "PEEK", "ease_in", "ease_out"}
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in functions:
                nodes.append(node)
            elif isinstance(node, ast.Assign):
                names = {n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)}
                if names and names <= constants:
                    nodes.append(node)
        cls.namespace = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), cls.namespace)

    def test_ctrl_alt_f(self):
        self.assertEqual(self.namespace["parse_hotkey"]("ctrl+alt+f"), (0x4003, ord("F")))

    def test_win_shift_f12(self):
        self.assertEqual(self.namespace["parse_hotkey"]("win+shift+f12"), (0x400C, 0x7B))

    def test_aliases_case_whitespace_and_empty(self):
        parse = self.namespace["parse_hotkey"]
        self.assertEqual(parse(" Control + ALT + f "), (0x4003, ord("F")))
        self.assertEqual(parse("super+f1"), (0x4008, 0x70))
        self.assertEqual(parse(None), (0x4000, 0))

    def test_easing_endpoints_and_midpoints(self):
        for name, midpoint in (("ease_in", 0.25), ("ease_out", 0.875)):
            ease = self.namespace[name]
            self.assertEqual(ease(0), 0)
            self.assertEqual(ease(1), 1)
            self.assertEqual(ease(0.5), midpoint)

    def test_tucked_position_scales_peek(self):
        self.namespace["S"] = {"top": 100, "h": 740, "scale": 1.5}
        self.assertEqual(self.namespace["tucked_y"](), 100 - 740 + int(self.namespace["PEEK"] * 1.5))

