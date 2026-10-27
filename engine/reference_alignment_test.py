"""Regression checks for mismatched legacy reference text and F5 clipping."""
import ast
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


TREE = ast.parse(Path(__file__).with_name("engine_server.py").read_text())


class ReferenceAlignmentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.meta = self.directory / "abcdef12.json"
        (self.directory / "abcdef12.wav").touch()
        legacy = next(n.value.value for n in TREE.body
                      if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LEGACY_REF_TEXT" for t in n.targets))
        self.model = types.SimpleNamespace(transcribe=Mock(return_value="lời nói thực tế trong file"))
        self.scope = {"json": json, "os": os, "voices_dir": lambda: self.directory,
                      "check_id": lambda vid: vid, "MODEL_LOCK": threading.Lock(),
                      "MODEL": self.model, "ARGS": types.SimpleNamespace(model="f5"),
                      "normalize_vi": lambda text: text, "LEGACY_REF_TEXT": legacy}
        function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "resolve_ref")
        exec(compile(ast.Module(body=[function], type_ignores=[]), "resolve_ref", "exec"), self.scope)

    def resolve(self, metadata):
        self.meta.write_text(json.dumps(metadata))
        return self.scope["resolve_ref"]({"custom_id": "abcdef12"})

    def test_legacy_template_is_replaced_with_actual_transcription(self):
        _, text = self.resolve({"ref_text": self.scope["LEGACY_REF_TEXT"]})
        self.assertEqual(text, "lời nói thực tế trong file")
        saved = json.loads(self.meta.read_text())
        self.assertEqual(saved["ref_text"], text)
        self.assertTrue(saved["ref_text_auto"])
        self.model.transcribe.assert_called_once()

    def test_verified_sample_script_is_preserved(self):
        _, text = self.resolve({"ref_text": self.scope["LEGACY_REF_TEXT"], "ref_text_verified": True})
        self.assertEqual(text, self.scope["LEGACY_REF_TEXT"])
        self.model.transcribe.assert_not_called()

    def test_arbitrary_manual_transcript_is_preserved(self):
        _, text = self.resolve({"ref_text": "câu nói tự nhiên của người trong file"})
        self.assertEqual(text, "câu nói tự nhiên của người trong file")
        self.model.transcribe.assert_not_called()

    def test_empty_asr_result_is_rejected_without_saving(self):
        self.model.transcribe.return_value = " "
        with self.assertRaisesRegex(ValueError, "Không nhận dạng được"):
            self.resolve({"ref_text": ""})
        self.assertEqual(json.loads(self.meta.read_text())["ref_text"], "")

    def _ref_scope(self):
        model = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "F5Model")
        method = next(n for n in model.body if isinstance(n, ast.FunctionDef) and n.name == "_ref")
        scope = {"os": os, "log": lambda *a, **k: None, "REF_MAX_SEC": 15.0}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "_ref", "exec"), scope)
        return scope

    def test_preprocessing_keeps_full_audio_and_rebuilds_when_text_changes(self):
        scope = self._ref_scope()
        preprocess = Mock(side_effect=lambda audio, text, **kw: (audio, text))
        fake_module = types.SimpleNamespace(preprocess_ref_audio_text=preprocess)
        # Mẫu ngắn: không cắt, không nhận dạng lại — giữ nguyên lời thoại người dùng.
        instance = types.SimpleNamespace(_refs={}, device="cpu", _duration=lambda path: 5.0,
                                         transcribe=Mock(return_value="không nên gọi"))
        audio = str(self.directory / "abcdef12.wav")
        with patch.dict(sys.modules, {"f5_tts.infer.utils_infer": fake_module}):
            scope["_ref"](instance, audio, "lời cũ")
            scope["_ref"](instance, audio, "lời cũ")
            _, text = scope["_ref"](instance, audio, "lời đã sửa")
        self.assertEqual(preprocess.call_count, 2)
        self.assertEqual(text, "lời đã sửa")
        self.assertFalse(preprocess.call_args.kwargs["clip_short"])
        instance.transcribe.assert_not_called()

    def test_long_reference_is_clipped_and_retranscribed(self):
        scope = self._ref_scope()
        calls = []

        def fake_pre(audio, text, **kw):
            calls.append((audio, text, kw.get("clip_short")))
            return (audio + "#clip" if kw.get("clip_short") else audio, text)

        preprocess = Mock(side_effect=fake_pre)
        fake_module = types.SimpleNamespace(preprocess_ref_audio_text=preprocess)
        # Mẫu dài (>15s): cắt ngắn rồi nhận dạng lại đúng đoạn đã cắt.
        instance = types.SimpleNamespace(_refs={}, device="cpu", _duration=lambda path: 30.0,
                                         transcribe=Mock(return_value="lời trong đoạn đã cắt"))
        audio = str(self.directory / "abcdef12.wav")
        with patch.dict(sys.modules, {"f5_tts.infer.utils_infer": fake_module}):
            clipped_audio, text = scope["_ref"](instance, audio, "lời gốc dài")
        instance.transcribe.assert_called_once()
        self.assertEqual([c[2] for c in calls], [True, False])  # cắt trước, rồi dựng bản cuối
        self.assertEqual(text, "lời trong đoạn đã cắt")
        self.assertEqual(clipped_audio, audio + "#clip")  # bản cuối dùng đoạn đã cắt
