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


class SpaceAfterPunctTest(unittest.TestCase):
    def setUp(self):
        self.sp = _load({"space_after_punct"})["space_after_punct"]

    def test_adds_space_after_punct_glued_to_word(self):
        self.assertEqual(self.sp("Câu một.Câu hai"), "Câu một. Câu hai")
        self.assertEqual(self.sp("a;b:c!d?e"), "a; b: c! d? e")
        self.assertEqual(self.sp("xin chào,bạn"), "xin chào, bạn")

    def test_keeps_existing_space(self):
        self.assertEqual(self.sp("Câu một. Câu hai."), "Câu một. Câu hai.")

    def test_protects_numbers(self):
        self.assertEqual(self.sp("giá 1.000.000 đồng"), "giá 1.000.000 đồng")
        self.assertEqual(self.sp("số pi là 3,14 nhé"), "số pi là 3,14 nhé")

    def test_number_then_word_still_spaced(self):
        # '.' sau số nhưng trước CHỮ (kết câu) vẫn được tách
        self.assertEqual(self.sp("tôi có 5.Cảm ơn"), "tôi có 5. Cảm ơn")


class ReadNumbersViTest(unittest.TestCase):
    def setUp(self):
        self.read = _load({"_vi_read_group", "_vi_read_int", "read_numbers_vi"},
                          {"_VI_ONES", "_VI_SCALE"})["read_numbers_vi"]

    def test_year(self):
        self.assertEqual(self.read("năm 1975 nhé"), "năm một nghìn chín trăm bảy mươi lăm nhé")

    def test_tens_special(self):
        self.assertEqual(self.read("21"), "hai mươi mốt")
        self.assertEqual(self.read("25"), "hai mươi lăm")
        self.assertEqual(self.read("15"), "mười lăm")

    def test_hundred_with_zero_tens(self):
        self.assertEqual(self.read("105"), "một trăm lẻ năm")

    def test_year_zero_hundreds_no_khong_tram(self):
        # Bỏ 'không trăm' (nghe lủng củng, model líu); giữ 'lẻ' khi hàng chục = 0
        self.assertEqual(self.read("2024"), "hai nghìn hai mươi bốn")
        self.assertEqual(self.read("2010"), "hai nghìn mười")
        self.assertEqual(self.read("2001"), "hai nghìn lẻ một")
        self.assertEqual(self.read("2000"), "hai nghìn")
        self.assertEqual(self.read("1000500"), "một triệu năm trăm")

    def test_thousand_separator(self):
        self.assertEqual(self.read("1.000.000"), "một triệu")
        self.assertEqual(self.read("150000"), "một trăm năm mươi nghìn")

    def test_decimal_comma(self):
        self.assertEqual(self.read("3,14"), "ba phẩy một bốn")

    def test_zero(self):
        self.assertEqual(self.read("số 0"), "số không")

    def test_no_number_unchanged(self):
        self.assertEqual(self.read("không có số nào"), "không có số nào")


@unittest.skipIf(np is None, "numpy không có trong python hệ thống")
class CrossfadeJoinTest(unittest.TestCase):
    def setUp(self):
        self.join = _load({"crossfade_join"})["crossfade_join"]

    def test_joins_without_inserting_silence(self):
        sr = 24000
        a = np.ones(sr, dtype=np.float32)
        b = np.ones(sr, dtype=np.float32)
        out = self.join([a, b], sr, 0.03)
        n = int(sr * 0.03)
        self.assertEqual(len(out), 2 * sr - n)          # chồng crossfade, không chèn lặng
        self.assertGreater(float(np.min(np.abs(out))), 0.9)  # không có vùng im lặng ở mối nối

    def test_single_segment_unchanged(self):
        sr = 24000
        a = np.ones(100, dtype=np.float32)
        self.assertEqual(len(self.join([a], sr)), 100)

    def test_empty_returns_tiny(self):
        self.assertEqual(len(self.join([], 24000)), 1)


@unittest.skipIf(np is None, "numpy không có trong python hệ thống")
class JoinVoxcpmTest(unittest.TestCase):
    def setUp(self):
        import os
        consts = [n for n in TREE.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "VOXCPM_PARA_GAP" for t in n.targets)]
        fns = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "join_voxcpm"]
        self.scope = {"re": re, "np": np, "os": os}
        exec(compile(ast.Module(body=[*consts, *fns], type_ignores=[]), "join", "exec"), self.scope)
        self.join = self.scope["join_voxcpm"]
        self.gap = self.scope["VOXCPM_PARA_GAP"]

    def test_para_boundary_inserts_pause(self):
        sr = 24000
        a = np.ones(sr, dtype=np.float32)
        b = np.ones(sr, dtype=np.float32)
        out = self.join([a, b], ["para", "para"], sr, 0.03)
        # hai đoạn nguyên độ dài + khoảng lặng VOXCPM_PARA_GAP ở giữa
        self.assertEqual(len(out), 2 * sr + int(sr * self.gap))
        mid = out[sr:sr + int(sr * self.gap)]
        self.assertEqual(float(np.max(np.abs(mid))), 0.0)   # giữa là im lặng
        # đầu đoạn sau được fade-in (mẫu đầu ~0), đuôi đoạn trước fade-out (mẫu cuối ~0)
        start2 = sr + int(sr * self.gap)
        self.assertLess(out[start2], 0.1)                   # fade-in đầu đoạn sau
        self.assertLess(out[sr - 1], 0.1)                   # fade-out đuôi đoạn trước

    def test_sent_boundary_crossfades(self):
        sr = 24000
        a = np.ones(sr, dtype=np.float32)
        b = np.ones(sr, dtype=np.float32)
        out = self.join([a, b], ["sent", "sent"], sr, 0.03)
        n = int(sr * 0.03)
        self.assertEqual(len(out), 2 * sr - n)              # crossfade, không chèn lặng
        self.assertGreater(float(np.min(np.abs(out))), 0.9)

    def test_single_segment_unchanged(self):
        sr = 24000
        a = np.ones(100, dtype=np.float32)
        self.assertEqual(len(self.join([a], ["para"], sr)), 100)


@unittest.skipIf(np is None, "numpy không có trong python hệ thống")
class CapInternalSilenceTest(unittest.TestCase):
    def setUp(self):
        import os
        consts = [n for n in TREE.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id in {"VOXCPM_MAXGAP", "VOXCPM_GAP_KEEP"} for t in n.targets)]
        fns = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "cap_internal_silence"]
        self.scope = {"re": re, "np": np, "os": os}
        exec(compile(ast.Module(body=[*consts, *fns], type_ignores=[]), "cap", "exec"), self.scope)
        self.cap = self.scope["cap_internal_silence"]

    def _tone(self, sr, secs):
        return (0.5 * np.sin(np.linspace(0, 300 * secs, int(sr * secs)))).astype(np.float32)

    def test_long_internal_gap_shortened(self):
        sr = 24000
        sig = np.concatenate([self._tone(sr, 0.5), np.zeros(int(sr * 0.6), np.float32), self._tone(sr, 0.5)])
        out = self.cap(sig, sr)
        self.assertLess(len(out), len(sig))                 # khoảng lặng dài đã bị rút
        self.assertLess(len(out), int(sr * 1.3))            # ~0.5 + 0.1 + 0.5 + lề
        self.assertGreaterEqual(len(out), int(sr * 1.05))   # vẫn giữ đủ hai đoạn tiếng

    def test_short_gap_preserved(self):
        sr = 24000
        sig = np.concatenate([self._tone(sr, 0.5), np.zeros(int(sr * 0.2), np.float32), self._tone(sr, 0.5)])
        out = self.cap(sig, sr)
        self.assertEqual(len(out), len(sig))                # ngắt ngắn (< ngưỡng) giữ nguyên

    def test_pure_tone_unchanged(self):
        sr = 24000
        sig = self._tone(sr, 1.0)
        self.assertEqual(len(self.cap(sig, sr)), len(sig))


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
