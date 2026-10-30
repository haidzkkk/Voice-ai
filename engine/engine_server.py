"""
Engine giọng AI trên máy cho Giọng AI.

Một tiến trình cho mỗi mô hình (--model):
  vieneu : VieNeu-TTS v3 Turbo — 25 giọng có sẵn + nhân bản giọng (nhanh)
  f5     : F5-TTS tiếng Việt (hynt/F5-TTS-Vietnamese-ViVoice) — rõ, giống giọng mẫu

Được app chính khởi chạy; tự thoát khi app chính đóng (stdin bị đóng).
Chỉ lắng nghe trên 127.0.0.1 và yêu cầu token.
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def add_runtime_dll_dirs():
    """Cho Windows tìm msvcp140.dll do gói msvc-runtime chép vào venv.

    Python không tìm DLL phụ thuộc trong thư mục venv, nên thiếu bước này thì
    onnxruntime/PyTorch vẫn báo "DLL load failed" trên máy chưa cài VC++
    Redistributable. Phải chạy trước mọi import thư viện C++ (kể cả numpy).
    """
    if os.name != "nt":
        return []
    handles = []
    for d in (sys.prefix, os.path.join(sys.prefix, "Scripts")):
        if os.path.isfile(os.path.join(d, "msvcp140.dll")):
            try:
                handles.append(os.add_dll_directory(d))
            except OSError:
                pass
    return handles


DLL_DIRS = add_runtime_dll_dirs()  # giữ tham chiếu: handle bị thu hồi thì thư mục bị gỡ

import numpy as np

ARGS = None
STATE = {"state": "loading", "error": "", "backend": "", "device": "", "detail": "Đang khởi động…"}
MODEL = None
MODEL_LOCK = threading.Lock()
JOBS = {}  # job id -> {"done", "total", "cancel"}
META = {}  # preset name -> metadata (gender, region, style, description, featured)

REGION_ORDER = {"Bắc": 0, "Trung": 1, "Nam": 2}
FAKE = bool(os.environ.get("GIONGAI_FAKE_ENGINE"))
LEGACY_REF_TEXT = "Xin chào, đây là giọng nói của tôi. Hôm nay trời rất đẹp, tôi muốn kể cho bạn nghe một câu chuyện thú vị về quê hương mình."
F5_EMOTIONS = {"vui", "buon", "gian", "haohung", "thitham", "kechuyen"}
# Khoảng lặng (giây) chèn giữa các cụm theo loại ngắt: cuối đoạn văn, cuối câu, sau dấu phẩy.
GAP = {"para": 0.45, "sent": 0.2}
# F5 ra giọng méo khi mẫu quá dài; mẫu dài hơn ngần này (giây) sẽ được cắt ngắn lại.
REF_MAX_SEC = 15.0


def log(*a):
    print("[engine]", *a, flush=True)


# ------------------------------------------------------------ chuẩn hóa ----

_NORM = None


def normalize_vi(text):
    """Đọc số, ngày giờ, ký hiệu thành chữ; trả về chữ thường (F5/IndexTTS cần)."""
    global _NORM
    try:
        if _NORM is None:
            from sea_g2p import Normalizer
            _NORM = Normalizer(lang="vi")
        out = _NORM.normalize(text)
        out = re.sub(r"</?en>", "", out)
    except Exception as e:
        log("không chuẩn hóa được văn bản:", e)
        out = text.lower()
    return re.sub(r"\s+", " ", out).strip()


# ------------------------------------------------------------- mô hình -----

def needs_fp32():
    """Card GTX 16xx / đời cũ hay lỗi khi chạy fp16/bf16 → dùng fp32 cho chắc."""
    if os.environ.get("GIONGAI_FP32"):
        return True
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        name = torch.cuda.get_device_name(0)
        major, minor = torch.cuda.get_device_capability(0)
        return "GTX 16" in name or (major, minor) < (7, 0) or "MX" in name
    except Exception:
        return False


def pick_torch_device():
    import torch
    if torch.cuda.is_available():
        return "cuda", "GPU " + torch.cuda.get_device_name(0)
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps", "Apple GPU"
    return "cpu", "CPU"


class FakeModel:
    """Mô hình giả để kiểm thử (GIONGAI_FAKE_ENGINE=1): sinh sóng sin."""
    sample_rate = 48000

    def list_preset_voices(self):
        return [(n, n) for n in META]

    def infer(self, text, voice=None, **kw):
        if isinstance(voice, str) and voice not in META:
            raise ValueError(f"Voice '{voice}' not found")
        n = int(self.sample_rate * max(0.3, len(text) * 0.06))
        t = np.arange(n) / self.sample_rate
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def encode_reference(self, path, denoise=True):
        import soundfile as sf
        wav, sr = sf.read(str(path))
        if len(wav) < sr:
            raise ValueError("clip quá ngắn")
        rng = np.random.default_rng(len(wav))
        return rng.standard_normal(192).astype(np.float32), rng.integers(0, 1000, size=(8, 50))


class VieNeuModel:
    def __init__(self, inner):
        self.m = inner
        self.sample_rate = inner.sample_rate

    def synth(self, text, voice):
        return self.m.infer(text, voice=voice)


class FakeCloneModel:
    """Giả lập F5/IndexTTS để kiểm thử."""
    sample_rate = 24000

    def synth(self, text, ref_wav, ref_text, speed=1.0):
        import soundfile as sf
        sf.info(ref_wav)  # kiểm tra file mẫu đọc được
        n = int(self.sample_rate * max(0.3, len(text) * 0.06) / max(speed, 0.1))
        t = np.arange(n) / self.sample_rate
        return (0.2 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)

    def transcribe(self, path):
        return "lời thoại nhận dạng tự động"


def download_model_file(repo, filename, **options):
    """Tải file mô hình, báo tiến độ byte thật (kể cả resume) qua /health.

    Mạng ở VN hay nghẽn giữa chừng khi tải file lớn từ huggingface.co, khiến tiến
    trình đứng im vô hạn. Hàm này làm cho việc tải không bao giờ treo:
    - Timeout đọc socket (Go đặt HF_HUB_DOWNLOAD_TIMEOUT) biến tình trạng nghẽn
      thành lỗi thay vì treo; hàm bắt lỗi đó và thử lại, tiếp tục từ chỗ dở.
    - Nếu máy chủ chính vẫn nghẽn, tự chuyển sang mirror HuggingFace để tải nốt.
    - Nếu phần đã tải phình vượt dung lượng thật (resume hỏng, server gửi lại từ
      đầu), xoá file .incomplete và tải lại sạch thay vì phình mãi tới vài chục GB.
    - Hết lượt thử thì ném lỗi rõ ràng để UI hiện nút "Thử lại" (tải tiếp từ chỗ dở).
    """
    import inspect
    import os
    import time
    import huggingface_hub
    from huggingface_hub import file_download, hf_hub_download, try_to_load_from_cache
    from tqdm.auto import tqdm

    _log = globals().get("log") or (lambda *a, **k: None)

    label = "/".join(filter(None, [options.get("subfolder"), filename]))
    if not options.get("force_download"):
        cached = try_to_load_from_cache(repo, label, cache_dir=options.get("cache_dir"),
                                        revision=options.get("revision"), repo_type=options.get("repo_type"))
        if isinstance(cached, str) and Path(cached).is_file():
            STATE.pop("download", None)
            STATE["detail"] = "Đang nạp file đã lưu trên máy: " + label
            return cached
    STATE["download"] = {"repo": repo, "file": label, "done": 0, "total": 0, "status": "downloading"}

    class _TooBig(Exception):
        """Đã nhận nhiều hơn dung lượng thật -> file .incomplete hỏng, cần tải lại sạch."""

    class DownloadProgress(tqdm):
        def __init__(self, *a, **kw):
            kw.pop("name", None)  # HF's tqdm adds this argument.
            kw["disable"] = False
            super().__init__(*a, **kw)
            self.publish()

        def display(self, *a, **kw):
            pass

        def publish(self):
            STATE["download"] = {
                "repo": repo, "file": label, "done": int(self.n),
                "total": int(self.total or 0), "status": "downloading"
            }

        def update(self, n=1):
            result = super().update(n)
            # Server bỏ qua Range và gửi lại từ đầu -> file dở sẽ phình mãi. Cắt sớm.
            if self.total and self.n > self.total * 1.05:
                raise _TooBig()
            self.publish()
            return result

    def fatal_errors():
        # Những lỗi không phải do mạng: thử lại cũng vô ích, phải ném ngay để nơi
        # gọi xử lý (ví dụ VieNeu bỏ qua metadata thiếu nhờ EntryNotFoundError).
        names = ("EntryNotFoundError", "RepositoryNotFoundError",
                 "RevisionNotFoundError", "GatedRepoError")
        out = []
        for modname in ("huggingface_hub.errors", "huggingface_hub.utils"):
            try:
                mod = __import__(modname, fromlist=["x"])
            except Exception:
                continue
            for n in names:
                cls = getattr(mod, n, None)
                if isinstance(cls, type) and cls not in out:
                    out.append(cls)
        return tuple(out)

    def set_endpoint(url):
        saved = [("env", os.environ.get("HF_ENDPOINT"))]
        try:
            os.environ["HF_ENDPOINT"] = url
        except Exception:
            pass
        for mod in (huggingface_hub, file_download, getattr(huggingface_hub, "constants", None)):
            try:
                if mod is not None and hasattr(mod, "ENDPOINT"):
                    saved.append((mod, mod.ENDPOINT))
                    mod.ENDPOINT = url
            except Exception:
                pass
        return saved

    def restore_endpoint(saved):
        for target, val in saved:
            try:
                if target == "env":
                    if val is None:
                        os.environ.pop("HF_ENDPOINT", None)
                    else:
                        os.environ["HF_ENDPOINT"] = val
                else:
                    target.ENDPOINT = val
            except Exception:
                pass

    def clear_incomplete():
        try:
            import glob
            from huggingface_hub.constants import HF_HUB_CACHE
            cache = options.get("cache_dir") or HF_HUB_CACHE
            try:
                from huggingface_hub.file_download import repo_folder_name
                folder = repo_folder_name(repo_id=repo, repo_type=options.get("repo_type") or "model")
            except Exception:
                folder = "models--" + repo.replace("/", "--")
            for p in glob.glob(os.path.join(cache, folder, "blobs", "*.incomplete")):
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass

    sig = inspect.signature(hf_hub_download)
    use_tqdm = "tqdm_class" in sig.parameters
    supports_endpoint = "endpoint" in sig.parameters

    def attempt(endpoint):
        kw = dict(options)
        patched = None
        if endpoint:
            if supports_endpoint:
                kw["endpoint"] = endpoint
            else:
                patched = set_endpoint(endpoint)
        try:
            if use_tqdm:
                return hf_hub_download(repo, filename, tqdm_class=DownloadProgress, **kw)
            # Bản F5 cũ dùng hub <1.0, không có tqdm_class.
            original = file_download.tqdm
            file_download.tqdm = DownloadProgress
            try:
                return hf_hub_download(repo, filename, **kw)
            finally:
                file_download.tqdm = original
        finally:
            if patched is not None:
                restore_endpoint(patched)

    # Thử CDN chính thức trước (ít lần để chuyển mirror sớm khi nghẽn), rồi mirror.
    mirror = os.environ.get("GIONGAI_HF_MIRROR", "https://hf-mirror.com").strip()
    plan = [(None, "máy chủ chính", 2)]
    if mirror and mirror != "-":
        plan.append((mirror, "mirror " + mirror, 4))

    fatal = fatal_errors()
    last = None
    for endpoint, where, tries in plan:
        for i in range(tries):
            try:
                path = attempt(endpoint)
                size = Path(path).stat().st_size
                STATE["download"] = {"repo": repo, "file": label, "done": size,
                                     "total": size, "status": "complete"}
                return path
            except fatal:
                STATE.pop("download", None)
                raise
            except _TooBig:
                _log("Tải", label, "bị gửi lặp lại ở", where, "- xoá file dở, tải lại sạch")
                clear_incomplete()
                last = RuntimeError("tải lại từ đầu vì file dở bị hỏng")
            except Exception as e:
                last = e
                _log("Tải", label, "lỗi ở", where, "-", type(e).__name__,
                     (str(e) or "")[:200], "- thử lại")
                time.sleep(min(20, 3 * (i + 1)))
            d = STATE.get("download") or {}
            STATE["download"] = {"repo": repo, "file": label,
                                 "done": int(d.get("done", 0)), "total": int(d.get("total", 0)),
                                 "status": "downloading"}

    STATE.pop("download", None)
    if isinstance(last, BaseException):
        raise last
    raise RuntimeError("Không tải được " + label)


def materialize_model_file(source):
    """Keep ONNX graphs and external data together as real files, not HF symlinks."""
    import hashlib
    import shutil
    import tempfile

    source = Path(source)
    # The snapshot parent identifies both repository revision and subfolder.
    key = hashlib.sha256(str(source.parent).encode()).hexdigest()[:16]
    directory = Path(ARGS.data) / "models" / "vieneu" / key
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    if target.is_file() and not target.is_symlink() and os.path.samefile(source, target):
        return target
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".model-")
    os.close(fd)
    try:
        os.unlink(temporary)
        try:
            os.link(source.resolve(), temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def fetch_onnx_artifacts(repo, files, subfolder):
    """Used by VieNeu for both its backbone and audio codec artifacts."""
    from huggingface_hub.errors import EntryNotFoundError

    directory = None
    for filename in files:
        STATE["detail"] = "Đang tải mô hình VieNeu: " + filename
        try:
            source = download_model_file(repo, filename, subfolder=subfolder or None)
        except EntryNotFoundError:
            if filename.endswith(".json") and filename != "config.json":
                continue  # Only optional metadata may be absent.
            raise
        target = materialize_model_file(source)
        if directory is not None and target.parent != directory:
            raise RuntimeError("Phiên bản mô hình thay đổi trong lúc tải. Hãy bấm Thử lại.")
        directory = target.parent
    STATE["detail"] = "Đã tải xong file mô hình. Đang nạp VieNeu…"
    return directory


def create_vieneu_model(options):
    from vieneu import Vieneu
    from vieneu._v3_turbo_engine.onnx_runtime_lite import OnnxV3LiteEngine

    original = OnnxV3LiteEngine._fetch
    OnnxV3LiteEngine._fetch = staticmethod(fetch_onnx_artifacts)
    try:
        return Vieneu(**options)
    finally:
        OnnxV3LiteEngine._fetch = staticmethod(original)


class _TooBigDirect(Exception):
    """Tải trực tiếp nhận quá dung lượng thật -> file dở bị hỏng, phải tải lại sạch."""


def _cleanup_hf_cache(repo):
    """Dọn cache HuggingFace cũ của repo (file .incomplete phình tới vài chục GB)."""
    import glob
    import shutil
    try:
        home = os.environ.get("HF_HOME")
        if not home:
            return
        folder = "models--" + repo.replace("/", "--")
        for base in (os.path.join(home, "hub"), home):
            path = os.path.join(base, folder)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def hf_download_direct(repo, filename, dest, label=None, optional=False):
    """Tải thẳng file lớn từ HuggingFace, tự chủ hoàn toàn để KHÔNG BAO GIỜ treo.

    hf_hub_download của HuggingFace có vòng thử-lại nội bộ: mạng ở VN hay nghẽn khi
    tải file lớn, và khi máy chủ trả NGUYÊN file (HTTP 200) thay vì phần còn thiếu
    (206) thì nó GHI NỐI vào file dở -> file phình mãi (đã thấy 24-33 GB cho model
    chỉ ~5,4 GB) rồi đứng im. Mình không chỉnh được vòng đó, nên tải thẳng bằng
    urllib: timeout đọc (nghẽn -> lỗi thay vì treo), resume ĐÚNG (ghi đè khi server
    trả 200), chặn phình theo dung lượng thật, thử lại có giới hạn, tự chuyển mirror.
    """
    import time
    import urllib.error
    import urllib.request

    label = label or filename
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    _log = globals().get("log") or (lambda *a, **k: None)

    mirror = os.environ.get("GIONGAI_HF_MIRROR", "https://hf-mirror.com").strip()
    bases = ["https://huggingface.co"]
    if mirror and mirror != "-" and mirror not in bases:
        bases.append(mirror)
    urls = [(b, b.rstrip("/") + "/" + repo + "/resolve/main/" + filename) for b in bases]

    def open_url(url, start):
        req = urllib.request.Request(url, headers={
            "User-Agent": "GiongAI", "Accept-Encoding": "identity"})
        if start:
            req.add_header("Range", "bytes=%d-" % start)
        return urllib.request.urlopen(req, timeout=30)

    def publish(done, total, status="downloading"):
        STATE["download"] = {"repo": repo, "file": label, "done": int(done),
                             "total": int(total or 0), "status": status}

    def probe_size():
        for _b, url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "GiongAI"})
                req.add_header("Range", "bytes=0-0")
                with urllib.request.urlopen(req, timeout=20) as r:
                    cr = r.getheader("Content-Range")
                    if cr and "/" in cr:
                        tail = cr.rsplit("/", 1)[1].strip()
                        if tail.isdigit():
                            return int(tail)
                    cl = r.getheader("Content-Length")
                    if cl and cl.isdigit() and getattr(r, "status", 200) == 200:
                        return int(cl)
            except Exception:
                continue
        return 0

    expected = probe_size()
    if dest.is_file() and expected and dest.stat().st_size == expected:
        publish(expected, expected, "complete")
        return str(dest)
    # Dọn cache HuggingFace cũ (file .incomplete phình) để lấy lại dung lượng trước khi tải.
    _cleanup_hf_cache(repo)
    publish(part.stat().st_size if part.exists() else 0, expected)

    last = None
    for base, url in urls:
        official = base.startswith("https://huggingface.co")
        where = "máy chủ chính" if official else "mirror " + base
        for attempt in range(2 if official else 5):
            try:
                have = part.stat().st_size if part.exists() else 0
                if expected and have > expected:  # file dở hỏng -> làm lại sạch
                    part.unlink()
                    have = 0
                if expected and have == expected:
                    os.replace(part, dest)
                    publish(expected, expected, "complete")
                    _cleanup_hf_cache(repo)
                    return str(dest)
                with open_url(url, have) as r:
                    resumed = getattr(r, "status", 200) == 206 and have > 0
                    start = have if resumed else 0
                    total = expected
                    cr = r.getheader("Content-Range")
                    if cr and "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
                        total = int(cr.rsplit("/", 1)[1].strip())
                    elif not total:
                        cl = r.getheader("Content-Length")
                        if cl and cl.isdigit():
                            total = start + int(cl)
                    done = start
                    with open(part, "r+b" if (resumed and part.exists()) else "wb") as f:
                        f.seek(start)
                        publish(done, total)
                        while True:
                            buf = r.read(1 << 20)
                            if not buf:
                                break
                            f.write(buf)
                            done += len(buf)
                            publish(done, total)
                            cap = total or expected
                            if cap and done > cap * 1.02:  # server gửi quá -> cắt
                                raise _TooBigDirect()
                size = part.stat().st_size
                if expected and size < expected:
                    raise IOError("tải thiếu %d/%d byte" % (size, expected))
                os.replace(part, dest)
                publish(size, size, "complete")
                _cleanup_hf_cache(repo)
                return str(dest)
            except urllib.error.HTTPError as e:
                # 404 = file không có trong kho: thử lại hay đổi mirror đều vô ích.
                if e.code == 404:
                    STATE.pop("download", None)
                    if optional:
                        return None
                    raise FileNotFoundError(label + " không có trên kho (404)")
                last = e
                _log("Tải", label, "lỗi ở", where, "-", type(e).__name__,
                     (str(e) or "")[:200], "- thử lại")
                time.sleep(min(20, 3 * (attempt + 1)))
            except _TooBigDirect:
                _log("Tải", label, "bị gửi lặp lại ở", where, "- xoá file dở, tải lại sạch")
                try:
                    part.unlink()
                except OSError:
                    pass
                last = RuntimeError("tải lại từ đầu vì file dở bị hỏng")
            except Exception as e:
                last = e
                _log("Tải", label, "lỗi ở", where, "-", type(e).__name__,
                     (str(e) or "")[:200], "- thử lại")
                time.sleep(min(20, 3 * (attempt + 1)))

    STATE.pop("download", None)
    if isinstance(last, BaseException):
        raise last
    raise RuntimeError("Không tải được " + label)


class F5Model:
    REPO = "hynt/F5-TTS-Vietnamese-ViVoice"
    VOCODER_REPO = "charactr/vocos-mel-24khz"
    ASR_REPO = "openai/whisper-large-v3-turbo"

    def __init__(self):
        from f5_tts.api import F5TTS
        self.device, self.device_name = pick_torch_device()
        mdir = Path(ARGS.data) / "models" / "f5"
        mdir.mkdir(parents=True, exist_ok=True)
        self._mdir = mdir
        STATE["detail"] = "Đang tải mô hình F5-TTS (model_last.pt ~5,4 GB, chỉ lần đầu)…"
        ckpt = hf_download_direct(self.REPO, "model_last.pt", mdir / "model_last.pt")
        vocab = mdir / "vocab.txt"
        if not vocab.exists():
            # Repo lưu bảng ký tự dưới tên config.json (theo hướng dẫn của tác giả)
            STATE["detail"] = "Đang tải bảng ký tự F5-TTS…"
            hf_download_direct(self.REPO, "config.json", vocab, label="vocab.txt")
        # Vocoder (vocos): F5TTS tự tải từ HuggingFace khi nạp, nhưng đường tải đó của
        # thư viện KHÔNG chống-treo -> trên mạng VN hay đứng im vô hạn NGAY LÚC KHỞI
        # ĐỘNG. Tải trước bằng trình tải chống-treo rồi trỏ vocoder_local_path để
        # F5TTS nạp thẳng từ đĩa, không còn chạm mạng.
        vdir = self._fetch_vocoder()
        STATE.pop("download", None)
        STATE["detail"] = "Đang nạp F5-TTS vào bộ nhớ để sử dụng…"
        self.m = F5TTS(model="F5TTS_Base", ckpt_file=ckpt, vocab_file=str(vocab),
                       vocoder_local_path=str(vdir), device=self.device)
        if self.device == "cuda" and needs_fp32():
            self.m.ema_model.float()
            self.device_name += " (fp32)"
        self.sample_rate = self.m.target_sample_rate
        self._refs = {}
        self._asr_ready = False

    def _fetch_vocoder(self):
        """Tải vocos về đĩa (config.yaml + pytorch_model.bin ~55 MB) để nạp cục bộ."""
        vdir = self._mdir / "vocos"
        vdir.mkdir(parents=True, exist_ok=True)
        STATE["detail"] = "Đang tải bộ tổng hợp âm thanh (vocoder ~55 MB, chỉ lần đầu)…"
        hf_download_direct(self.VOCODER_REPO, "config.yaml", vdir / "config.yaml",
                           label="vocoder/config.yaml")
        hf_download_direct(self.VOCODER_REPO, "pytorch_model.bin", vdir / "pytorch_model.bin",
                           label="vocoder/pytorch_model.bin")
        return vdir

    def _ensure_asr(self):
        """Tải trước Whisper rồi nạp cục bộ, để việc nhận dạng lời thoại không bao
        giờ kích hoạt đường tải HuggingFace mặc định (hay treo trên mạng VN)."""
        if self._asr_ready:
            return
        import f5_tts.infer.utils_infer as infer
        if getattr(infer, "asr_pipe", None) is not None:
            self._asr_ready = True
            return
        wdir = self._mdir / "whisper"
        wdir.mkdir(parents=True, exist_ok=True)
        STATE["detail"] = "Đang tải bộ nhận dạng giọng nói (Whisper ~1,6 GB, chỉ lần đầu)…"
        required = ["config.json", "model.safetensors", "preprocessor_config.json",
                    "tokenizer.json", "tokenizer_config.json"]
        optional = ["generation_config.json", "vocab.json", "merges.txt",
                    "special_tokens_map.json", "added_tokens.json", "normalizer.json"]
        for name in required:
            hf_download_direct(self.ASR_REPO, name, wdir / name, label="whisper/" + name)
        for name in optional:
            hf_download_direct(self.ASR_REPO, name, wdir / name, label="whisper/" + name, optional=True)
        STATE.pop("download", None)
        STATE["detail"] = "Đang nạp bộ nhận dạng giọng nói…"
        import torch
        from transformers import pipeline
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        # Trỏ thẳng vào thư mục đã tải -> transformers nạp cục bộ, không gọi mạng.
        infer.asr_pipe = pipeline("automatic-speech-recognition", model=str(wdir),
                                  torch_dtype=dtype, device=torch.device(self.device))
        self._asr_ready = True

    def _duration(self, path):
        import soundfile as sf
        info = sf.info(str(path))
        return info.frames / float(info.samplerate or 1)

    def _ref(self, ref_wav, ref_text):
        from f5_tts.infer.utils_infer import preprocess_ref_audio_text
        quiet = lambda *a, **k: None
        st = os.stat(ref_wav)
        key = (ref_wav, st.st_mtime, st.st_size, ref_text)
        if key not in self._refs:
            wav_path, text = ref_wav, ref_text
            # Mẫu quá dài làm F5 ra giọng méo ("ma mị") và dễ đọc thừa chữ. Cắt còn
            # ~12s theo điểm lặng tự nhiên rồi NHẬN DẠNG LẠI đúng đoạn đã cắt để lời
            # thoại khớp với audio — đây là nguồn gốc chính của giọng méo và chữ thừa.
            # Mẫu đã ngắn (kèm lời thoại người dùng tự nhập) giữ nguyên, không đụng tới.
            try:
                if self._duration(ref_wav) > REF_MAX_SEC:
                    clipped, _ = preprocess_ref_audio_text(ref_wav, "x", show_info=quiet,
                                                            clip_short=True, device=self.device)
                    detected = self.transcribe(clipped)
                    if detected.strip():
                        wav_path, text = clipped, detected
            except Exception as e:
                log("không cắt ngắn được giọng mẫu, dùng nguyên bản:", e)
            self._refs[key] = preprocess_ref_audio_text(wav_path, text, show_info=quiet,
                                                         clip_short=False, device=self.device)
        return self._refs[key]

    def transcribe(self, path):
        STATE["detail"] = "Đang chuẩn bị bộ nhận dạng giọng nói (chỉ lần đầu)…"
        try:
            self._ensure_asr()
            import soundfile as sf
            # App reference clips are WAV. Passing a filename makes Transformers
            # invoke ffmpeg even for WAV; raw samples bypass that dependency.
            samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
            mono = samples.mean(axis=1)
            if sample_rate != 16000:
                from math import gcd
                from scipy.signal import resample_poly
                factor = gcd(sample_rate, 16000)
                mono = resample_poly(mono, 16000 // factor, sample_rate // factor)
            audio = {"raw": mono, "sampling_rate": 16000}
            return normalize_vi(self.m.transcribe(audio, language="vi"))
        finally:
            STATE["detail"] = ""

    def synth(self, text, ref_wav, ref_text, speed=1.0):
        from f5_tts.infer.utils_infer import infer_process
        ra, rt = self._ref(ref_wav, ref_text)
        wav, _sr, _spec = infer_process(ra, rt, text, self.m.ema_model, self.m.vocoder, self.m.mel_spec_type,
                                        show_info=lambda *a, **k: None, progress=None, speed=speed,
                                        nfe_step=32, device=self.device)
        return np.asarray(wav, dtype=np.float32).reshape(-1)


class VoxCPMModel:
    """VoxCPM2 (OpenBMB, Apache-2.0) — clone giọng đa ngữ từ mẫu + lời thoại.
    Dùng chung cơ chế giọng cá nhân/cảm xúc với F5 nên interface giống hệt:
    synth(text, ref_wav, ref_text). Không kèm bộ nhận dạng (ASR) nên không tự
    transcribe được; lời thoại mẫu phải do người dùng nhập (hoặc dùng F5).

    Lưu ý runtime: tên tham số generate() và cách chuyển thiết bị lấy theo
    README VoxCPM; có thể cần chỉnh nhẹ theo phiên bản gói cài thực tế (chỉ test
    được trên máy có GPU)."""
    REPO = "openbmb/VoxCPM2"

    def __init__(self):
        from voxcpm import VoxCPM
        self.device, self.device_name = pick_torch_device()
        STATE["detail"] = "Đang tải VoxCPM2 (vài GB, chỉ lần đầu)…"
        try:
            self.m = VoxCPM.from_pretrained(self.REPO, load_denoiser=False)
        except TypeError:
            self.m = VoxCPM.from_pretrained(self.REPO)
        inner = getattr(self.m, "tts_model", self.m)
        try:
            if self.device != "cpu" and hasattr(inner, "to"):
                inner.to(self.device)
        except Exception as e:
            log("VoxCPM: không chuyển được sang", self.device, "- dùng mặc định:", e)
            self.device_name += " (mặc định)"
        self.sample_rate = int(getattr(inner, "sample_rate", 16000))

    def synth(self, text, ref_wav, ref_text, speed=1.0):
        # speed bỏ qua ở đây: VoxCPM không nhận tham số tốc độ; tốc độ do lớp
        # synthesize() xử lý bằng librosa time-stretch sau khi ghép.
        wav = self.m.generate(text=text, prompt_wav_path=ref_wav, prompt_text=ref_text,
                              cfg_value=2.0, inference_timesteps=10, normalize=False, denoise=False)
        return np.asarray(wav, dtype=np.float32).reshape(-1)

    def transcribe(self, path):
        raise ValueError("VoxCPM2 không tự nhận dạng lời thoại. Hãy nhập đúng lời "
                         "của mẫu, hoặc dùng F5-TTS để tự nhận dạng.")


def load_meta():
    try:
        import vieneu
        p = Path(vieneu.__file__).parent / "assets" / "voices_v3_turbo.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        for name, v in data.get("presets", {}).items():
            META[name] = {
                "description": v.get("description", ""),
                "gender": v.get("gender", ""),
                "region": v.get("region", ""),
                "style": v.get("style", ""),
                "featured": v.get("featured"),
            }
    except Exception as e:
        log("không đọc được metadata giọng:", e)


def load_vieneu():
    load_meta()
    if FAKE:
        return VieNeuModel(FakeModel()), "fake", "CPU"
    STATE["detail"] = "Đang tải mô hình (lần đầu sẽ tải khoảng vài trăm MB, có thể mất vài phút)…"
    gpu_kw = {"dtype": "float32"} if needs_fp32() else {}
    attempts = [] if ARGS.force_cpu else [gpu_kw]
    attempts.append({"backend": "onnx"})
    last = None
    for kw in attempts:
        try:
            m = create_vieneu_model(kw)
            backend = getattr(m, "backend", "onnx")
            device = "CPU"
            if backend == "pytorch":
                try:
                    device = pick_torch_device()[1]
                except Exception:
                    device = "GPU"
            return VieNeuModel(m), backend, device
        except Exception as e:
            last = e
            log("load failed:", e)
            traceback.print_exc()
    raise RuntimeError(f"Không tải hoặc nạp được mô hình: {last}")


def load_model():
    global MODEL
    try:
        if ARGS.model == "vieneu":
            MODEL, backend, device = load_vieneu()
        elif FAKE:
            MODEL, backend, device = FakeCloneModel(), "fake", "CPU"
        elif ARGS.model == "f5":
            m = F5Model()
            MODEL, backend, device = m, "f5", m.device_name
        elif ARGS.model == "voxcpm":
            m = VoxCPMModel()
            MODEL, backend, device = m, "voxcpm", m.device_name
        else:
            raise ValueError("mô hình không hợp lệ: " + ARGS.model)
        STATE.update(state="ready", backend=backend, device=device, detail="", error="")
        STATE.pop("download", None)
        log("model ready:", ARGS.model, backend, device)
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if "out of memory" in msg.lower():
            msg = "Card màn hình không đủ bộ nhớ (VRAM) cho mô hình này. " + msg
        STATE.update(state="error", error=msg, detail="")
        STATE.pop("download", None)


# ---------------------------------------------------------- giọng cá nhân ---

def voices_dir():
    d = Path(getattr(ARGS, "voices_dir", None) or (Path(ARGS.data) / "voices"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def check_id(vid):
    if not re.fullmatch(r"[a-f0-9]{8,32}", vid or ""):
        raise ValueError("mã giọng không hợp lệ")
    return vid


def list_custom():
    out = []
    for p in sorted(voices_dir().glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": p.stem, "name": d.get("name", p.stem), "gender": d.get("gender", ""),
                        "created": d.get("created", 0), "seconds": d.get("seconds", 0),
                        "ref_text": d.get("ref_text", ""),
                        "ref_text_auto": d.get("ref_text_auto", False),
                        "emotion_samples": d.get("emotion_samples", {}),
                        "ref_text_legacy": d.get("ref_text", "").strip() == LEGACY_REF_TEXT and not d.get("ref_text_verified")})
        except Exception:
            pass
    out.sort(key=lambda x: -x["created"])
    return out


def load_custom_vieneu(vid):
    d = json.loads((voices_dir() / f"{check_id(vid)}.json").read_text(encoding="utf-8"))
    if not d.get("speaker_emb"):
        raise ValueError("Giọng này chưa được phân tích cho VieNeu")
    codes = d.get("codes")
    return {
        "speaker_emb": np.asarray(d["speaker_emb"], dtype=np.float32),
        "codes": None if codes is None else np.asarray(codes, dtype=np.int64),
    }


# --------------------------------------------------------------- synth -----

def split_groups(text, limit=700):
    text = re.sub(r"[ \t]+", " ", text.replace("\r", "")).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    groups = []
    for p in paras:
        sents = re.split(r"(?<=[.!?…;:])\s+", p)
        cur = ""
        for s in sents:
            if cur and len(cur) + len(s) + 1 > limit:
                groups.append((cur, False))
                cur = s
            else:
                cur = (cur + " " + s).strip()
        if cur:
            groups.append((cur, True))  # True = kết thúc đoạn văn
    return groups


def f5_fine_groups(text, limit=700):
    """Chia theo CÂU cho F5 (không tách theo dấu phẩy): mỗi câu là một cụm và
    được đưa NGUYÊN VẸN vào mô hình. Tách sẵn theo dấu phẩy khiến F5 sinh những
    cụm quá ngắn, độc lập → dễ đọc thiếu/thừa chữ; giữ cả câu để mô hình tự xử
    lý nhịp trong câu. Chỉ chèn khoảng lặng ở ranh giới câu/đoạn. Câu quá ngắn
    được gộp vào câu sau để tránh vụn.

    Trả (cụm, loại nghỉ sau cụm) với loại trong GAP: "para", "sent".
    """
    out = []
    for part, para_end in split_groups(text, limit):
        sents = [s.strip() for s in re.split(r"(?<=[.!?…;:])\s+", part) if s.strip()]
        for si, sent in enumerate(sents):
            last_sent = si == len(sents) - 1
            kind = "para" if (last_sent and para_end) else "sent"
            out.append((sent, kind))
    merged = []
    for seg, kind in out:
        if merged and len(merged[-1][0]) < 6:  # câu quá ngắn ("Dạ.", "Ừ.") gộp vào câu sau
            merged[-1] = ((merged[-1][0] + " " + seg).strip(), kind)
        else:
            merged.append((seg, kind))
    return merged


def encode(wav, sr, fmt="mp3"):
    pcm = (np.clip(wav, -1, 1) * 32767).astype(np.int16)
    if fmt == "mp3":
        try:
            import lameenc
            enc = lameenc.Encoder()
            enc.set_bit_rate(128)
            enc.set_in_sample_rate(int(sr))
            enc.set_channels(1)
            enc.set_quality(2)
            return bytes(enc.encode(pcm.tobytes()) + enc.flush()), "mp3"
        except Exception as e:
            log("không mã hóa được MP3, dùng WAV:", e)
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, pcm, int(sr), format="WAV", subtype="PCM_16")
    return buf.getvalue(), "wav"


def resolve_ref(req):
    """Giọng mẫu cho F5/IndexTTS: (đường dẫn wav, lời thoại)."""
    ref_wav = req.get("ref_wav") or ""
    ref_text = (req.get("ref_text") or "").strip()
    vid = req.get("custom_id")
    if vid:
        meta_path = voices_dir() / f"{check_id(vid)}.json"
        ref_wav = str(voices_dir() / f"{vid}.wav")
        d = json.loads(meta_path.read_text(encoding="utf-8"))
        ref_text = (d.get("ref_text") or "").strip()
        legacy = ref_text == LEGACY_REF_TEXT and not d.get("ref_text_verified")
        if (not ref_text or legacy) and ARGS.model == "f5":
            with MODEL_LOCK:
                ref_text = MODEL.transcribe(ref_wav)
            if not ref_text.strip():
                raise ValueError("Không nhận dạng được lời thoại. Nghe lại mẫu và nhập đúng lời nói trong mục Sửa lời thoại.")
            d["ref_text"] = ref_text
            d["ref_text_auto"] = True
            meta_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    if not ref_wav or not os.path.exists(ref_wav):
        raise ValueError("Không tìm thấy giọng mẫu")
    if ARGS.model == "voxcpm" and not ref_text:
        raise ValueError("VoxCPM2 cần lời thoại của mẫu. Hãy nhập đúng lời trong mẫu ở mục Sửa lời thoại.")
    if ARGS.model == "f5":
        ref_text = normalize_vi(ref_text)
    return ref_wav, ref_text


def f5_style_groups(text, samples):
    """Style labels select reference clips; labels never enter model text."""
    tag = "macdinh"
    position = 0
    groups = []
    for match in re.finditer(r"\[([\w]+)\]", text):
        label = match.group(1).lower()
        if label != "macdinh" and label not in F5_EMOTIONS:
            raise ValueError(f"Thẻ [{label}] chưa hỗ trợ. Dùng các thẻ trong mục Cảm xúc F5-TTS.")
        if label != "macdinh" and not samples.get(label):
            raise ValueError(f"Chưa thiết lập mẫu cho [{label}]. Mở Thiết lập cảm xúc để chọn mẫu trước.")
        groups.extend((part, kind, tag) for part, kind in f5_fine_groups(text[position:match.start()]))
        tag, position = label, match.end()
    groups.extend((part, kind, tag) for part, kind in f5_fine_groups(text[position:]))
    if not groups:
        raise ValueError("Hãy nhập lời cần đọc sau thẻ cảm xúc.")
    return groups


def synthesize(req, job):
    text = (req.get("text") or "").strip()
    if not text:
        raise ValueError("Văn bản trống")
    groups = [(part, "para" if end else "sent", "macdinh") for part, end in split_groups(text)]
    speed_pct = int(req.get("speed") or 0)
    native_speed = 1.0

    if ARGS.model == "vieneu":
        voice = load_custom_vieneu(req.get("voice")) if req.get("custom") else (req.get("voice") or None)
        run = lambda g: MODEL.synth(g, voice)
    else:
        samples = {}
        if req.get("custom_id"):
            metadata = json.loads((voices_dir() / (check_id(req["custom_id"]) + ".json")).read_text(encoding="utf-8"))
            samples = metadata.get("emotion_samples") or {}
        groups = f5_style_groups(text, samples)
        references = {}
        for _, _, tag in groups:
            if tag in references:
                continue
            reference = req if tag == "macdinh" else {"custom_id": check_id(samples[tag])}
            try:
                references[tag] = resolve_ref(reference)
            except FileNotFoundError:
                raise ValueError(f"Mẫu cho [{tag}] không còn trên máy. Hãy thiết lập lại cảm xúc này.")
        if ARGS.model == "f5":
            native_speed = max(0.5, min(2.0, 1 + speed_pct / 100.0))
            speed_pct = 0
            run = lambda g, tag: MODEL.synth(normalize_vi(g), *references[tag], native_speed)
        else:  # voxcpm: đưa text thô (model tự chuẩn hoá đa ngữ); tốc độ xử lý bằng librosa bên dưới
            run = lambda g, tag: MODEL.synth(g, *references[tag])

    job["total"] = len(groups)
    parts = []
    for i, (g, kind, tag) in enumerate(groups):
        if job.get("cancel"):
            raise InterruptedError("đã hủy")
        with MODEL_LOCK:
            wav = run(g) if ARGS.model == "vieneu" else run(g, tag)
        sr = MODEL.sample_rate
        parts.append(np.asarray(wav, dtype=np.float32).reshape(-1))
        if i < len(groups) - 1:
            parts.append(np.zeros(int(sr * GAP.get(kind, 0.2)), dtype=np.float32))
        job["done"] = i + 1
    sr = MODEL.sample_rate
    wav = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)

    if speed_pct:
        try:
            import librosa
            wav = librosa.effects.time_stretch(wav, rate=max(0.5, 1 + speed_pct / 100.0))
        except Exception as e:
            log("không đổi được tốc độ:", e)
    vol = int(req.get("volume") or 0)
    if vol:
        wav = wav * (1 + vol / 100.0)
    peak = float(np.max(np.abs(wav))) if wav.size else 0
    if peak > 0.99:
        wav = wav * (0.99 / peak)
    return encode(wav, sr, req.get("format") or "mp3")


def clone(req):
    """Lưu giọng cá nhân. VieNeu còn phân tích sẵn đặc trưng giọng."""
    name = (req.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Hãy đặt tên cho giọng")
    raw = base64.b64decode(req.get("wav_b64") or "")
    if len(raw) < 1000:
        raise ValueError("File ghi âm trống hoặc quá ngắn")
    import soundfile as sf
    vid = uuid.uuid4().hex[:16]
    wav_path = voices_dir() / f"{vid}.wav"
    wav_path.write_bytes(raw)
    try:
        data, sr = sf.read(str(wav_path))
        seconds = len(data) / sr
        if seconds < 2.5:
            raise ValueError(f"Đoạn ghi âm chỉ {seconds:.1f} giây, cần ít nhất 3 giây")
        rec = {"name": name, "gender": req.get("gender", ""), "created": int(time.time()),
               "seconds": round(seconds, 1), "ref_text": (req.get("ref_text") or "").strip()[:500]}
        rec["ref_text_verified"] = bool(rec["ref_text"])
        if ARGS.model == "vieneu":
            with MODEL_LOCK:
                emb, codes = MODEL.m.encode_reference(str(wav_path), denoise=bool(req.get("denoise", True)))
            rec["speaker_emb"] = [round(float(x), 6) for x in np.asarray(emb).reshape(-1)]
            rec["codes"] = None if codes is None else np.asarray(codes, dtype=int).tolist()
        (voices_dir() / f"{vid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return {"id": vid, "name": name}
    except Exception:
        wav_path.unlink(missing_ok=True)
        raise


def update_voice(req):
    vid = check_id(req.get("id"))
    p = voices_dir() / f"{vid}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    if "ref_text" in req:
        d["ref_text"] = (req.get("ref_text") or "").strip()[:500]
        d.pop("ref_text_auto", None)
        d["ref_text_verified"] = bool(d["ref_text"])
    if req.get("name"):
        d["name"] = req["name"].strip()[:60]
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


def transcribe_reference(req):
    """Recognize the exact saved/uploaded sample without altering its voice metadata."""
    if ARGS.model != "f5":
        raise ValueError("Hãy cài và khởi động F5-TTS để tự nhận dạng lời thoại")
    if req.get("id"):
        path = voices_dir() / (check_id(req["id"]) + ".wav")
        with MODEL_LOCK:
            text = MODEL.transcribe(str(path))
    else:
        import tempfile
        raw = base64.b64decode(req.get("wav_b64") or "", validate=True)
        if len(raw) < 1000:
            raise ValueError("Chưa có đoạn ghi âm để nhận dạng")
        with tempfile.TemporaryDirectory(dir=ARGS.data, prefix="transcribe-") as directory:
            path = Path(directory) / "reference.wav"
            path.write_bytes(raw)
            with MODEL_LOCK:
                text = MODEL.transcribe(str(path))
    if not text.strip():
        raise ValueError("Không nhận dạng được lời thoại. Hãy chọn đoạn có giọng nói rõ hoặc nhập lời thoại thủ công.")
    return {"ref_text": text}


# ----------------------------------------------------------------- http ----

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self.headers.get("X-Token") != ARGS.token:
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, {**STATE, "model": ARGS.model})
        if u.path == "/voices":
            presets = []
            if MODEL is not None and ARGS.model == "vieneu":
                for _, name in MODEL.m.list_preset_voices():
                    presets.append({"id": name, **META.get(name, {})})
                presets.sort(key=lambda v: (REGION_ORDER.get(v.get("region"), 9),
                                            v.get("featured") is None, v.get("featured") or 0))
            return self._send(200, {"presets": presets, "custom": list_custom()})
        if u.path == "/progress":
            j = JOBS.get((q.get("job") or [""])[0])
            return self._send(200, {"done": j["done"], "total": j["total"]} if j else {})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/cancel":
                j = JOBS.get((q.get("job") or [""])[0])
                if j:
                    j["cancel"] = True
                return self._send(200, {"ok": True})
            if u.path == "/voice-update":
                return self._send(200, update_voice(self._body()))
            if STATE["state"] != "ready":
                return self._send(503, {"error": STATE.get("error") or "Mô hình chưa sẵn sàng"})
            if u.path == "/tts":
                req = self._body()
                jid = req.get("job") or uuid.uuid4().hex
                job = JOBS[jid] = {"done": 0, "total": 0, "cancel": False}
                try:
                    data, ext = synthesize(req, job)
                finally:
                    threading.Timer(600, lambda: JOBS.pop(jid, None)).start()
                return self._send(200, data, "audio/mpeg" if ext == "mp3" else "audio/wav", {"X-Ext": ext})
            if u.path == "/clone":
                return self._send(200, clone(self._body()))
            if u.path == "/transcribe":
                return self._send(200, transcribe_reference(self._body()))
            self._send(404, {"error": "not found"})
        except InterruptedError:
            self._send(499, {"error": "canceled"})
        except Exception as e:
            traceback.print_exc()
            msg = str(e)
            if "out of memory" in msg.lower():
                msg = "Card màn hình hết bộ nhớ (VRAM). Hãy thử văn bản ngắn hơn hoặc dùng mô hình nhẹ hơn. " + msg
            self._send(500, {"error": msg})


def watch_parent():
    if os.name == "nt" and watch_parent_pipe_windows():
        os._exit(0)
    try:
        while sys.stdin.read(1):
            pass
    except Exception:
        pass
    os._exit(0)


def watch_parent_pipe_windows():
    """Chờ app đóng stdin mà KHÔNG đọc chặn trên pipe.

    Trên Windows, một lệnh đọc đồng bộ đang chờ trên pipe stdin làm treo mọi
    lệnh GetFileType(stdin) khác. Một số DLL (vd. OpenBLAS của scipy) gọi hàm
    đó khi khởi tạo runtime C trong lúc đang giữ loader lock -> import treo vĩnh
    viễn, kéo theo cả luồng HTTP (không tạo được luồng mới). Hỏi pipe định kỳ
    bằng PeekNamedPipe thì không có lệnh đọc nào chờ. Trả False nếu stdin không
    phải pipe để dùng cách cũ.
    """
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetFileType.argtypes = [wintypes.HANDLE]
        k32.GetFileType.restype = wintypes.DWORD
        k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.PeekNamedPipe.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        if k32.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
            return False
    except Exception:
        return False
    avail = wintypes.DWORD()
    while k32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
        time.sleep(0.5)
    return True  # pipe bị đóng (app tắt engine hoặc app đã thoát)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--voices-dir")
    ap.add_argument("--model", default="vieneu", choices=["vieneu", "f5", "voxcpm"])
    ap.add_argument("--force-cpu", action="store_true")
    ap.add_argument("--no-watch", action="store_true")
    ARGS = ap.parse_args()
    if not ARGS.no_watch:
        threading.Thread(target=watch_parent, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.port), H)
    threading.Thread(target=load_model, daemon=True).start()
    log("listening on", ARGS.port, "model", ARGS.model)
    srv.serve_forever()


if __name__ == "__main__":
    main()
