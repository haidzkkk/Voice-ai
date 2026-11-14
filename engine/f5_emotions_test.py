"""Mô tả cách đọc {…}: tách text theo marker, gắn mô tả cho từng đoạn, marker
không lọt vào text đọc; {} rỗng về mặc định (ô 'Mô tả cách đọc')."""
import ast
import re
import unittest
from pathlib import Path


class DescSpansTest(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).with_name("engine_server.py").read_text())
        names = {"split_groups", "coarse_groups", "desc_spans"}
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.scope = {"re": re}
        exec(compile(ast.Module(body=[*functions], type_ignores=[]), "desc", "exec"), self.scope)

    def spans(self, text, global_desc=""):
        return self.scope["desc_spans"](text, global_desc)

    def test_no_marker_uses_global_desc(self):
        groups = self.spans("Xin chào các bạn. Hẹn gặp lại.", "giọng vui")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][2], "giọng vui")
        self.assertNotIn("{", groups[0][0])

    def test_markers_split_and_carry_descriptions(self):
        groups = self.spans("{vui vẻ} Làm được rồi! {trầm buồn} Nhưng rồi phải đi.")
        descs = [d for _, _, d in groups]
        self.assertEqual(descs, ["vui vẻ", "trầm buồn"])
        self.assertTrue(all("{" not in text and "}" not in text for text, _, _ in groups))
        self.assertIn("Làm được rồi!", groups[0][0])
        self.assertIn("Nhưng rồi phải đi.", groups[1][0])

    def test_empty_marker_returns_to_default(self):
        groups = self.spans("{thì thầm} Bí mật nhé. {} Nói bình thường lại.", "giọng kể chuyện")
        self.assertEqual([d for _, _, d in groups], ["thì thầm", "giọng kể chuyện"])

    def test_text_before_first_marker_uses_global(self):
        groups = self.spans("Mở đầu đây. {giận dữ} Sao lại thế!", "điềm tĩnh")
        self.assertEqual(groups[0][2], "điềm tĩnh")
        self.assertEqual(groups[1][2], "giận dữ")

    def test_empty_text_raises(self):
        with self.assertRaisesRegex(ValueError, "Hãy nhập"):
            self.spans("   ")


if __name__ == "__main__":
    unittest.main()
