"""resolve_ref trả về đường dẫn wav của giọng mẫu (custom_id hoặc ref_wav preset),
báo lỗi nếu thiếu. VoxCPM không cần lời thoại của mẫu (reference mode)."""
import ast
import os
import tempfile
import unittest
from pathlib import Path

TREE = ast.parse(Path(__file__).with_name("engine_server.py").read_text())


class ResolveRefTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.scope = {"os": os, "voices_dir": lambda: self.directory, "check_id": lambda vid: vid}
        fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "resolve_ref")
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "resolve_ref", "exec"), self.scope)

    def resolve(self, req):
        return self.scope["resolve_ref"](req)

    def test_custom_id_returns_voice_wav_path(self):
        (self.directory / "abcdef12.wav").touch()
        self.assertEqual(self.resolve({"custom_id": "abcdef12"}),
                         str(self.directory / "abcdef12.wav"))

    def test_preset_ref_wav_is_used_when_no_custom_id(self):
        wav = self.directory / "preset.wav"
        wav.touch()
        self.assertEqual(self.resolve({"ref_wav": str(wav)}), str(wav))

    def test_missing_sample_raises(self):
        with self.assertRaisesRegex(ValueError, "Không tìm thấy giọng mẫu"):
            self.resolve({"custom_id": "abcdef99"})

    def test_no_voice_raises(self):
        with self.assertRaisesRegex(ValueError, "Không tìm thấy giọng mẫu"):
            self.resolve({})


class ResolveRefF5Test(unittest.TestCase):
    """F5 trả (wav, lời thoại mẫu đã chuẩn hoá). Chỉ kiểm nhánh preset (ref_wav +
    ref_text) — không chạm MODEL/Whisper."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.scope = {"os": os, "voices_dir": lambda: self.directory, "check_id": lambda vid: vid,
                      "normalize_vi": lambda t: t.lower(), "LEGACY_REF_TEXT": "legacy"}
        fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "resolve_ref_f5")
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "resolve_ref_f5", "exec"), self.scope)

    def test_preset_returns_wav_and_normalized_text(self):
        wav = self.directory / "preset.wav"
        wav.touch()
        got = self.scope["resolve_ref_f5"]({"ref_wav": str(wav), "ref_text": "Xin Chào"})
        self.assertEqual(got, (str(wav), "xin chào"))

    def test_missing_wav_raises(self):
        with self.assertRaisesRegex(ValueError, "Không tìm thấy giọng mẫu"):
            self.scope["resolve_ref_f5"]({"ref_text": "x"})


if __name__ == "__main__":
    unittest.main()
