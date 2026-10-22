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


class _FakeResp:
    def __init__(self, body, status, headers):
        self._body, self.status, self._headers, self._pos = body, status, headers, 0

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, n=-1):
        if n is None or n < 0:
            chunk, self._pos = self._body[self._pos:], len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_server(data, honor_range=True):
    """Giả lập huggingface.co qua urllib: hỗ trợ probe 0-0, resume 206, và ca
    server bỏ qua Range trả nguyên file 200."""
    length = len(data)

    def urlopen(req, timeout=None):
        rng = req.get_header("Range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[len("bytes="):].partition("-")
            start = int(start_s or 0)
            if start == 0 and end_s == "0":  # probe dung lượng
                return _FakeResp(data[:1], 206,
                                 {"Content-Range": "bytes 0-0/%d" % length, "Content-Length": "1"})
            if honor_range:
                body = data[start:]
                return _FakeResp(body, 206, {
                    "Content-Range": "bytes %d-%d/%d" % (start, length - 1, length),
                    "Content-Length": str(len(body))})
        return _FakeResp(data, 200, {"Content-Length": str(length)})  # bỏ qua Range

    return urlopen


class DirectDownloadTest(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).with_name("engine_server.py").read_text()
        function = next(n for n in ast.parse(source).body
                        if isinstance(n, ast.FunctionDef) and n.name == "hf_download_direct")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scope = {
            "Path": Path, "os": os, "STATE": {},
            "_TooBigDirect": type("_TooBigDirect", (Exception,), {}),
            "_cleanup_hf_cache": lambda *a, **k: None,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), source, "exec"), self.scope)
        self.data = bytes(range(256)) * 400  # 102400 byte
        env = patch.dict(os.environ, {"GIONGAI_HF_MIRROR": "-"})  # chỉ dùng máy chủ chính
        env.start()
        self.addCleanup(env.stop)

    def run_download(self, honor_range=True):
        dest = self.root / "model_last.pt"
        with patch("urllib.request.urlopen", _fake_server(self.data, honor_range)), \
                patch("time.sleep"):
            result = self.scope["hf_download_direct"]("repo/name", "model_last.pt", dest)
        return Path(result), dest

    def test_full_download(self):
        result, dest = self.run_download()
        self.assertEqual(result, dest)
        self.assertEqual(dest.read_bytes(), self.data)
        self.assertEqual(self.scope["STATE"]["download"]["status"], "complete")
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_resume_appends_from_partial(self):
        part = self.root / "model_last.pt.part"
        part.write_bytes(self.data[:40000])
        result, dest = self.run_download(honor_range=True)
        self.assertEqual(dest.read_bytes(), self.data)  # nối đúng, không hỏng

    def test_server_ignoring_range_overwrites_instead_of_growing(self):
        # Đây là gốc lỗi 24-33 GB: server trả nguyên file (200) cho yêu cầu resume.
        part = self.root / "model_last.pt.part"
        part.write_bytes(self.data[:40000])
        result, dest = self.run_download(honor_range=False)
        self.assertEqual(dest.stat().st_size, len(self.data))  # KHÔNG phình
        self.assertEqual(dest.read_bytes(), self.data)

    def test_oversized_partial_is_discarded(self):
        part = self.root / "model_last.pt.part"
        part.write_bytes(self.data + b"x" * 5000)  # lớn hơn dung lượng thật
        result, dest = self.run_download(honor_range=True)
        self.assertEqual(dest.stat().st_size, len(self.data))
        self.assertEqual(dest.read_bytes(), self.data)

    def test_reuses_existing_complete_file_without_network(self):
        dest = self.root / "model_last.pt"
        dest.write_bytes(self.data)

        def boom(*a, **k):
            raise AssertionError("đã có file đủ, không được tải lại")

        # probe_size vẫn gọi mạng một lần để biết dung lượng; cho phép probe, chặn tải.
        calls = {"n": 0}

        def urlopen(req, timeout=None):
            calls["n"] += 1
            rng = req.get_header("Range")
            if rng == "bytes=0-0":
                return _FakeResp(self.data[:1], 206,
                                 {"Content-Range": "bytes 0-0/%d" % len(self.data)})
            return boom()

        with patch("urllib.request.urlopen", urlopen), patch("time.sleep"):
            result = self.scope["hf_download_direct"]("repo/name", "model_last.pt", dest)
        self.assertEqual(Path(result), dest)
        self.assertEqual(self.scope["STATE"]["download"]["status"], "complete")

    def test_optional_missing_file_returns_none_without_retrying(self):
        # File phụ (vd. added_tokens.json của Whisper) không tồn tại -> 404. Phải trả
        # None ngay, không thử lại/đổi mirror, và không ném lỗi làm hỏng cả luồng.
        import urllib.error
        calls = {"n": 0}

        def urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", urlopen), patch("time.sleep"):
            result = self.scope["hf_download_direct"](
                "repo/name", "added_tokens.json", self.root / "added_tokens.json", optional=True)
        self.assertIsNone(result)
        self.assertLessEqual(calls["n"], 2)  # probe + 1 lần tải, không lặp lại

    def test_required_missing_file_raises_clear_error(self):
        import urllib.error

        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", urlopen), patch("time.sleep"):
            with self.assertRaises(FileNotFoundError):
                self.scope["hf_download_direct"](
                    "repo/name", "model_last.pt", self.root / "model_last.pt")
