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

import numpy as np

ARGS = None
STATE = {"state": "loading", "error": "", "backend": "", "device": "", "detail": "Đang khởi động…"}
MODEL = None
MODEL_LOCK = threading.Lock()
JOBS = {}  # job id -> {"done", "total", "cancel"}
META = {}  # preset name -> metadata (gender, region, style, description, featured)

REGION_ORDER = {"Bắc": 0, "Trung": 1, "Nam": 2}
FAKE = bool(os.environ.get("GIONGAI_FAKE_ENGINE"))


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
    """Expose real byte progress, including resumed downloads, through /health."""
    import inspect
    from huggingface_hub import file_download, hf_hub_download
    from tqdm.auto import tqdm

    label = "/".join(filter(None, [options.get("subfolder"), filename]))
    STATE["download"] = {"repo": repo, "file": label, "done": 0, "total": 0, "status": "downloading"}

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
            self.publish()
            return result

    try:
        if "tqdm_class" in inspect.signature(hf_hub_download).parameters:
            path = hf_hub_download(repo, filename, tqdm_class=DownloadProgress, **options)
        else:
            # Existing F5 installations use hub <1.0, without tqdm_class.
            original = file_download.tqdm
            file_download.tqdm = DownloadProgress
            try:
                path = hf_hub_download(repo, filename, **options)
            finally:
                file_download.tqdm = original
        size = Path(path).stat().st_size
        STATE["download"] = {"repo": repo, "file": label, "done": size, "total": size, "status": "complete"}
        return path
    except Exception:
        STATE.pop("download", None)
        raise


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


class F5Model:
    REPO = "hynt/F5-TTS-Vietnamese-ViVoice"

    def __init__(self):
        import shutil
        from f5_tts.api import F5TTS
        self.device, self.device_name = pick_torch_device()
        STATE["detail"] = "Đang tải mô hình F5-TTS tiếng Việt (~1,4 GB, chỉ lần đầu)…"
        ckpt = download_model_file(self.REPO, "model_last.pt")
        mdir = Path(ARGS.data) / "models" / "f5"
        mdir.mkdir(parents=True, exist_ok=True)
        vocab = mdir / "vocab.txt"
        if not vocab.exists():
            # Repo lưu bảng ký tự dưới tên config.json (theo hướng dẫn của tác giả)
            shutil.copy(download_model_file(self.REPO, "config.json"), vocab)
        STATE["detail"] = "Đã tải xong file mô hình. Đang nạp F5-TTS…"
        self.m = F5TTS(model="F5TTS_Base", ckpt_file=ckpt, vocab_file=str(vocab), device=self.device)
        if self.device == "cuda" and needs_fp32():
            self.m.ema_model.float()
            self.device_name += " (fp32)"
        self.sample_rate = self.m.target_sample_rate
        self._refs = {}

    def _ref(self, ref_wav, ref_text):
        from f5_tts.infer.utils_infer import preprocess_ref_audio_text
        st = os.stat(ref_wav)
        key = (ref_wav, st.st_mtime, st.st_size, ref_text)
        if key not in self._refs:
            self._refs[key] = preprocess_ref_audio_text(ref_wav, ref_text, show_info=lambda *a, **k: None,
                                                         device=self.device)
        return self._refs[key]

    def transcribe(self, path):
        STATE["detail"] = "Đang tải bộ nhận dạng giọng nói (chỉ lần đầu)…"
        try:
            return normalize_vi(self.m.transcribe(path, language="vi"))
        finally:
            STATE["detail"] = ""

    def synth(self, text, ref_wav, ref_text, speed=1.0):
        from f5_tts.infer.utils_infer import infer_process
        ra, rt = self._ref(ref_wav, ref_text)
        wav, _sr, _spec = infer_process(ra, rt, text, self.m.ema_model, self.m.vocoder, self.m.mel_spec_type,
                                        show_info=lambda *a, **k: None, progress=None, speed=speed,
                                        nfe_step=32, device=self.device)
        return np.asarray(wav, dtype=np.float32).reshape(-1)


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
    d = Path(ARGS.data) / "voices"
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
                        "ref_text": d.get("ref_text", "")})
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
        if not ref_text and ARGS.model == "f5":
            with MODEL_LOCK:
                ref_text = MODEL.transcribe(ref_wav)
            d["ref_text"] = ref_text
            d["ref_text_auto"] = True
            meta_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    if not ref_wav or not os.path.exists(ref_wav):
        raise ValueError("Không tìm thấy giọng mẫu")
    if ARGS.model == "f5":
        ref_text = normalize_vi(ref_text)
    return ref_wav, ref_text


def synthesize(req, job):
    text = (req.get("text") or "").strip()
    if not text:
        raise ValueError("Văn bản trống")
    groups = split_groups(text)
    job["total"] = len(groups)
    speed_pct = int(req.get("speed") or 0)
    native_speed = 1.0

    if ARGS.model == "vieneu":
        voice = load_custom_vieneu(req.get("voice")) if req.get("custom") else (req.get("voice") or None)
        run = lambda g: MODEL.synth(g, voice)
    else:
        ref_wav, ref_text = resolve_ref(req)
        if ARGS.model == "f5":
            native_speed = max(0.5, min(2.0, 1 + speed_pct / 100.0))
            speed_pct = 0
        run = lambda g: MODEL.synth(normalize_vi(g), ref_wav, ref_text, native_speed)

    parts = []
    for i, (g, para_end) in enumerate(groups):
        if job.get("cancel"):
            raise InterruptedError("đã hủy")
        with MODEL_LOCK:
            wav = run(g)
        sr = MODEL.sample_rate
        parts.append(np.asarray(wav, dtype=np.float32).reshape(-1))
        if i < len(groups) - 1:
            parts.append(np.zeros(int(sr * (0.45 if para_end else 0.2)), dtype=np.float32))
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
    if req.get("name"):
        d["name"] = req["name"].strip()[:60]
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


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
    try:
        while sys.stdin.read(1):
            pass
    except Exception:
        pass
    os._exit(0)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", default="vieneu", choices=["vieneu", "f5"])
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
