"""F5 style routing must strip labels and never silently substitute missing samples."""
import ast
import re
import unittest
from pathlib import Path


class F5EmotionRoutingTest(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).with_name("engine_server.py").read_text())
        names = {"split_groups", "f5_fine_groups", "f5_style_groups"}
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "F5_EMOTIONS" for t in n.targets))
        self.scope = {"re": re}
        exec(compile(ast.Module(body=[assignment, *functions], type_ignores=[]), "style_groups", "exec"), self.scope)

    def groups(self, text):
        return self.scope["f5_style_groups"](text, {"vui": "abcdef12", "buon": "abcdef34"})

    def test_emotion_persists_across_paragraphs_and_can_reset(self):
        groups = self.groups("Xin chào. [vui] Làm được rồi!\nThật tuyệt! [buon] Tôi nhớ bạn. [macdinh] Hẹn gặp lại.")
        self.assertEqual([tag for _, _, tag in groups], ["macdinh", "vui", "vui", "buon", "macdinh"])
        self.assertTrue(all("[" not in text for text, _, _ in groups))
        self.assertEqual(groups[-1][0], "Hẹn gặp lại.")

    def test_normal_text_uses_default_reference(self):
        self.assertEqual(self.groups("Nội dung tự nhiên."), [("Nội dung tự nhiên.", "para", "macdinh")])

    def test_unconfigured_emotion_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Chưa thiết lập mẫu"):
            self.groups("[gian] Tôi đang nói.")

    def test_unknown_tag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "chưa hỗ trợ"):
            self.groups("[happy] Xin chào.")

    def test_tags_without_spoken_text_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "nhập lời"):
            self.groups("[vui][buon]")

    def test_repeated_and_uppercase_tags_preserve_text(self):
        self.assertEqual(self.groups("[VUI][vui] Một câu."), [("Một câu.", "para", "vui")])
