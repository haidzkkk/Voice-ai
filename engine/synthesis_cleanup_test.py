"""Chống hallucination F5/VoxCPM: không để cụm quá ngắn (bẫy local_speed=0.3) và
cắt đuôi năng lượng thấp của mỗi đoạn sinh ra."""
import ast
import re
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # numpy chỉ có trong venv engine; test chia cụm không cần nó
    np = None

TREE = ast.parse(Path(__file__).with_name("engine_server.py").read_text())


def _load(names, const_names=()):
    functions = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    consts = [n for n in TREE.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in const_names for t in n.targets)]
    scope = {"re": re, "np": np}
    exec(compile(ast.Module(body=[*consts, *functions], type_ignores=[]), "cleanup", "exec"), scope)
    return scope


class CoarseGroupsTest(unittest.TestCase):
    def setUp(self):
        self.scope = _load({"split_groups", "coarse_groups"})

    def test_keeps_whole_paragraph_unsplit(self):
        # Chế độ không-cắt-câu: nhiều câu trong một đoạn → MỘT cụm (không tách theo ".").
        groups = self.scope["coarse_groups"]("Câu một. Câu hai. Câu ba.")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][1], "para")

    def test_splits_on_blank_line(self):
        groups = self.scope["coarse_groups"]("Đoạn một ở đây.\n\nĐoạn hai ở đây.")
        self.assertEqual(len(groups), 2)


@unittest.skipIf(np is None, "numpy không có trong python hệ thống")
class TrimTrailingSilenceTest(unittest.TestCase):
    def setUp(self):
        self.trim = _load({"trim_trailing_silence"})["trim_trailing_silence"]

    def test_trims_low_energy_tail(self):
        sr = 24000
        speech = np.sin(np.linspace(0, 100, sr)).astype(np.float32)  # 1s "có tiếng"
        tail = np.zeros(sr, dtype=np.float32)                        # 1s im lặng
        out = self.trim(np.concatenate([speech, tail]), sr)
        self.assertLess(len(out), int(1.5 * sr))        # đã cắt phần lớn đuôi
        self.assertGreaterEqual(len(out), sr)           # giữ nguyên phần có tiếng
        self.assertLessEqual(len(out), len(speech) + int(0.1 * sr))

    def test_keeps_all_loud_audio(self):
        sr = 24000
        loud = (0.5 * np.sin(np.linspace(0, 200, 2 * sr))).astype(np.float32)
        out = self.trim(loud, sr)
        self.assertEqual(len(out), len(loud))

    def test_silent_input_unchanged(self):
        sr = 24000
        silent = np.zeros(sr, dtype=np.float32)
        out = self.trim(silent, sr)
        self.assertEqual(len(out), len(silent))

    def test_tiny_input_unchanged(self):
        sr = 24000
        tiny = np.ones(10, dtype=np.float32)
        self.assertEqual(len(self.trim(tiny, sr)), 10)


if __name__ == "__main__":
    unittest.main()
