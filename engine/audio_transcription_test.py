"""Regression checks for passing decoded WAV to F5's Whisper pipeline."""
import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class AudioTranscriptionTest(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).with_name("engine_server.py").read_text())
        model = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "F5Model")
        method = next(n for n in model.body if isinstance(n, ast.FunctionDef) and n.name == "transcribe")
        self.scope = {"STATE": {}, "normalize_vi": lambda text: text}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "transcription", "exec"), self.scope)

    def test_decoded_mono_samples_are_used_instead_of_filename(self):
        samples = Mock()
        samples.mean.return_value = [0.1, 0.2]
        soundfile = types.SimpleNamespace(read=Mock(return_value=(samples, 48000)))
        signal = types.SimpleNamespace(resample_poly=Mock(return_value=[0.3]))
        transcriber = Mock(return_value="xin chào")
        model = types.SimpleNamespace(m=types.SimpleNamespace(transcribe=transcriber))
        with patch.dict(sys.modules, {"soundfile": soundfile, "scipy.signal": signal}):
            result = self.scope["transcribe"](model, Path("reference.wav"))
        soundfile.read.assert_called_once_with("reference.wav", dtype="float32", always_2d=True)
        samples.mean.assert_called_once_with(axis=1)
        signal.resample_poly.assert_called_once_with([0.1, 0.2], 1, 3)
        transcriber.assert_called_once_with({"raw": [0.3], "sampling_rate": 16000}, language="vi")
        self.assertEqual(result, "xin chào")
        self.assertEqual(self.scope["STATE"]["detail"], "")

    def test_read_failure_clears_busy_status(self):
        model = types.SimpleNamespace(m=Mock())
        soundfile = types.SimpleNamespace(read=Mock(side_effect=OSError("invalid audio")))
        with patch.dict(sys.modules, {"soundfile": soundfile}):
            with self.assertRaises(OSError):
                self.scope["transcribe"](model, "invalid.wav")
        model.m.transcribe.assert_not_called()
        self.assertEqual(self.scope["STATE"]["detail"], "")
