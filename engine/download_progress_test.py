"""Run with python3 -m unittest discover -s engine -p '*_test.py'.

Test download reporting without downloading models or installing ML libraries.
"""
import ast
import os
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
        self.hub.try_to_load_from_cache = lambda *a, **kw: None
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
                             {"repo": "repo", "file": "model.pt", "done": 10, "total": 10, "status": "complete"})
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
        with patch("time.sleep"), self.assertRaises(OSError):
            self.scope["download_model_file"]("repo", "model.pt")
        self.assertNotIn("download", self.scope["STATE"])
        self.assertIs(self.download.tqdm, FakeTqdm)

    def test_transient_error_is_retried_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.pt"
            path.write_bytes(b"0123456789")
            calls = {"n": 0}

            def fetch(repo, filename):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("network interrupted")
                self.download.tqdm(total=10, initial=10)
                return str(path)

            self.hub.hf_hub_download = fetch
            with patch("time.sleep"):
                result = self.scope["download_model_file"]("repo", "model.pt")
            self.assertEqual(result, str(path))
            self.assertEqual(calls["n"], 2)
            self.assertEqual(self.scope["STATE"]["download"]["status"], "complete")

    def test_runaway_resend_is_cut_and_retried_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.pt"
            path.write_bytes(b"0123456789")
            calls = {"n": 0}

            def fetch(repo, filename):
                calls["n"] += 1
                progress = self.download.tqdm(total=10, initial=0)
                if calls["n"] == 1:
                    progress.update(20)  # server gửi quá dung lượng -> bị cắt (_TooBig)
                    return str(path)  # không bao giờ tới đây
                progress.update(10)
                return str(path)

            self.hub.hf_hub_download = fetch
            with patch("time.sleep"):
                result = self.scope["download_model_file"]("repo", "model.pt")
            self.assertEqual(result, str(path))
            self.assertEqual(calls["n"], 2)

    def test_existing_cache_skips_network_and_download_indicator(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.pt"
            path.write_bytes(b"cached weights")
            self.hub.try_to_load_from_cache = lambda *a, **kw: str(path)
            def fetch(*a, **kw):
                self.fail("Cached model must not contact the network")
            self.hub.hf_hub_download = fetch
            self.scope["STATE"]["download"] = {"status": "complete"}
            self.assertEqual(self.scope["download_model_file"]("repo", "model.pt"), str(path))
            self.assertNotIn("download", self.scope["STATE"])
            self.assertIn("đã lưu", self.scope["STATE"]["detail"])


class ModelMaterializationTest(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).with_name("engine_server.py").read_text()
        function = next(n for n in ast.parse(source).body
                        if isinstance(n, ast.FunctionDef) and n.name == "materialize_model_file")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.scope = {"Path": Path, "os": os, "ARGS": types.SimpleNamespace(data=str(self.root))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), source, "exec"), self.scope)

    def test_symlinked_graph_and_data_stay_inside_same_model_directory(self):
        snapshot = self.root / "snapshot" / "onnx_update"
        snapshot.mkdir(parents=True)
        outputs = []
        for name in ("vieneu_prefill.onnx", "vieneu_backbone_shared.data"):
            blob = self.root / "blobs" / name / "hash"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(name.encode())
            link = snapshot / name
            link.symlink_to(blob)
            target = self.scope["materialize_model_file"](link)
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.resolve().parent, target.parent)
            self.assertEqual(target.read_bytes(), blob.read_bytes())
            self.assertTrue(os.path.samefile(target, blob))  # No duplicate large weights.
            self.assertEqual(self.scope["materialize_model_file"](link), target)
            self.assertTrue(link.is_symlink())  # Preserve the original cache.
            outputs.append(target)
        self.assertEqual(outputs[0].parent, outputs[1].parent)

    def test_copy_fallback_when_hard_links_are_unavailable(self):
        source = self.root / "graph.onnx"
        source.write_bytes(b"model")
        with patch("os.link", side_effect=OSError("cross-device link")):
            target = self.scope["materialize_model_file"](source)
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), b"model")
        self.assertEqual(list(target.parent.glob(".model-*")), [])
