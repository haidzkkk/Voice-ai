"""Run with python3 -m unittest discover -s engine -p '*_test.py'.

Test download reporting without downloading models or installing ML libraries.
"""
import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeTqdm:
    def __init__(self, total=None, initial=0, **kwargs):
        self.total, self.n = total, initial

    def update(self, n=1):
        self.n += n


class DownloadProgressTest(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).with_name("engine_server.py").read_text()
        function = next(n for n in ast.parse(source).body
                        if isinstance(n, ast.FunctionDef) and n.name == "download_model_file")
        self.scope = {"STATE": {}, "Path": Path}
        exec(compile(ast.Module(body=[function], type_ignores=[]), source, "exec"), self.scope)
        self.hub = types.ModuleType("huggingface_hub")
        self.download = types.SimpleNamespace(tqdm=FakeTqdm)
        self.hub.file_download = self.download
        self.modules = patch.dict(sys.modules, {
            "huggingface_hub": self.hub,
            "tqdm.auto": types.SimpleNamespace(tqdm=FakeTqdm),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_resumed_download_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.pt"
            path.write_bytes(b"0123456789")

            def fetch(repo, filename):
                progress = self.download.tqdm(total=10, initial=4)
                self.assertEqual(self.scope["STATE"]["download"]["done"], 4)
                progress.update(3)
                self.assertEqual(self.scope["STATE"]["download"]["done"], 7)
                return str(path)

            self.hub.hf_hub_download = fetch
            self.scope["download_model_file"]("repo", "model.pt")
            self.assertEqual(self.scope["STATE"]["download"],
                             {"file": "model.pt", "done": 10, "total": 10})
            self.assertIs(self.download.tqdm, FakeTqdm)

    def test_cached_file_with_modern_api(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b"{}")

            def fetch(repo, filename, tqdm_class=None):
                self.assertTrue(issubclass(tqdm_class, FakeTqdm))
                return str(path)  # Cache hits do not construct a progress bar.

            self.hub.hf_hub_download = fetch
            self.scope["download_model_file"]("repo", "config.json")
            self.assertEqual(self.scope["STATE"]["download"]["done"], 2)

    def test_failure_clears_progress_and_restores_legacy_hook(self):
        def fetch(repo, filename):
            raise OSError("network interrupted")

        self.hub.hf_hub_download = fetch
        with self.assertRaises(OSError):
            self.scope["download_model_file"]("repo", "model.pt")
        self.assertNotIn("download", self.scope["STATE"])
        self.assertIs(self.download.tqdm, FakeTqdm)
