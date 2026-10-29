# FLOW — Kiến trúc & cách thêm model TTS mới

Tài liệu dev. README.md là hướng dẫn cho người dùng; file này mô tả **luồng bên trong** và **các điểm cần sửa khi thêm một engine/model TTS mới**.

---

## 1. Tổng quan kiến trúc

```
┌─────────────────────────┐
│  Trình duyệt (UI)       │  web/index.html  (HTML + JS thuần, 1 file)
└───────────┬─────────────┘
            │ HTTP (chỉ 127.0.0.1 / localhost)
┌───────────▼─────────────┐
│  Go server              │  main.go  — HTTP, job, file, UI
│                         │  tts.go   — engine Microsoft (Edge TTS, WebSocket)
│                         │  engine.go— cài & điều khiển engine Python (local)
│                         │  storage*.go — cấu hình & chuyển thư mục dữ liệu
└───────────┬─────────────┘
            │ HTTP nội bộ (token) + spawn process
┌───────────▼─────────────┐
│  Python engine          │  engine/engine_server.py
│  (1 process / model)    │  nạp model, tổng hợp, clone giọng, transcribe
└─────────────────────────┘
```

Có **2 lớp engine**:

| Loại | ID (`engine` trong request) | Chạy ở | Code |
|------|------------------------------|--------|------|
| Microsoft (online) | `""` (UI gọi là `ms`) | Cloud Microsoft | `tts.go` |
| Local AI | `vieneu`, `f5` | Python trên máy | `engine.go` + `engine_server.py` |

> Thêm model mới gần như luôn là **thêm một engine local** → đi theo nhánh Python. Phần dưới tập trung vào việc đó.

---

## 2. Luồng tạo audio (runtime)

1. UI gửi `POST /api/tts` với body `ttsRequest` (`main.go:75`):
   `{text, voice, engine, custom, rate, volume, preview}`.
   - `engine=""` → Microsoft; `engine="vieneu"|"f5"` → local.
   - `custom=true` → giọng cá nhân (voice là ID 8–32 hex).
2. `/api/tts` handler (`main.go:231`) **validate**:
   - thẻ biểu cảm `[cười]…` chỉ cho `vieneu` (`emotionCuePattern`),
   - thẻ cảm xúc `[vui]…` cần `f5` + `custom` (`f5StylePattern`),
   - engine local phải đang chạy (`currentEngine(id) != nil`).
3. `startJob()` (`main.go:118`) tạo **job async** (ID 12 ký tự), trả ngay. UI poll `GET /api/job?id=`.
4. Trong goroutine:
   - local → `synthEngine()` (`engine.go:783`) → gọi `POST /tts` sang Python.
   - Microsoft → `Synthesize()` (`tts.go`).
5. Ghi file ra `outDir` (hoặc `engine/previews/` nếu `preview=true`, có cache theo hash input).
6. UI lấy file qua `/files/<name>` (hoặc `/preview/<name>`), lịch sử qua `/api/history`.

Tiến độ: `synthEngine` poll `GET /progress?job=` của Python mỗi 700ms; hủy → `POST /cancel?job=`.

---

## 3. Vòng đời engine Python (`engine.go`)

- **Khai báo**: `modelDefs` (`engine.go:49`) — mỗi model 1 entry `{ID, Name, Venv, Marker, Python}`.
- **Cài**: `doInstall()` (`engine.go:451`) → tải `uv` → cài Python → tạo venv → cài torch + thư viện riêng theo `switch m.ID` → ghi script `engine_server.py` ra đĩa → ghi **marker** (`installed-*.json`) → `startEngine`.
- **Chạy**: `startEngine()` (`engine.go:587`) spawn `python engine_server.py --port --token --data --voices-dir --model <id>`. Mỗi model 1 process, cổng tự do, xác thực bằng token. **Đóng stdin = engine tự thoát.**
- **Gọi**: `engCall/engJSON/engAudio` — HTTP tới `127.0.0.1:<port>` kèm token.
- **Trạng thái**: `modelStatus()` (`engine.go:836`) gộp installed/running/health cho UI qua `/api/engine/status`.

Giao tiếp Go → Python (các path trong `engine_server.py` `do_GET/do_POST`):
`GET /health`, `GET /voices`, `GET /progress`, `POST /tts`, `POST /clone`, `POST /transcribe`, `POST /voice-update`, `POST /cancel`.

---

## 4. Hợp đồng (interface) của một Model trong Python

Model được chọn bằng `--model <id>`, nạp trong `load_model()` (`engine_server.py:749`), và **phải cung cấp**:

```python
class MyModel:
    sample_rate: int          # Hz của wav trả về

    # BẮT BUỘC — trả về np.ndarray float32 (mono) cho 1 đoạn text.
    # Chữ ký tùy loại (xem synthesize()):
    #   kiểu VieNeu : synth(self, text, voice)
    #   kiểu F5/clone: synth(self, text, ref_wav, ref_text, speed=1.0)
    def synth(self, ...): ...

    # TÙY CHỌN — chỉ khi hỗ trợ clone giọng cá nhân từ mẫu audio:
    def transcribe(self, path) -> str: ...   # nhận dạng lời thoại mẫu
    device_name: str                          # tên thiết bị để hiển thị
```

`synthesize()` (`engine_server.py:928`) điều phối: cắt text (`split_groups`), gọi `MODEL.synth` dưới `MODEL_LOCK`, chèn khoảng lặng `GAP` giữa các đoạn, đổi tốc độ/âm lượng, chuẩn hóa đỉnh, rồi `encode()` ra mp3/wav.

---

## 5. ✅ Checklist thêm một model mới (ví dụ id `index`)

### A. Go — `engine.go`
1. **`modelDefs`** (`:49`): thêm
   ```go
   "index": {ID: "index", Name: "IndexTTS", Venv: "venv-index", Marker: "installed-index.json", Python: "3.11"},
   ```
2. **`doInstall()` switch** (`:487`): thêm `case "index":` cài torch (`installTorch`) + các `pip install` thư viện của model (+ `runtimePackages` cho Windows). Dùng `setStep(...)` để UI hiện bước.
3. **`synthEngine()`** (`:783`): thêm nhánh dựng request body cho model mới — quyết định model nhận `voice/custom`, hay `custom_id`, hay `ref_wav+ref_text` (giống F5). Nếu cần giọng mẫu từ VieNeu, tái dùng `presetRef()`.

### B. Go — `main.go`
4. **`startJob()`** (`:123`): mở rộng `local := in.Engine == "vieneu" || in.Engine == "f5"` để gồm `"index"`.
5. **`/api/tts` validate** (`:245`): nếu model có thẻ/ràng buộc riêng, thêm regex + kiểm tra (theo mẫu `emotionCuePattern`/`f5StylePattern`). Check `currentEngine` đã bao id mới nhờ bước 4.
6. (Nếu `previewName` cần trộn thêm metadata như F5 — `:88` — thì bổ sung.)

### C. Python — `engine/engine_server.py`
7. **Viết class model** theo hợp đồng ở mục 4 (có `FakeCloneModel` làm mẫu để test không cần tải model thật).
8. **`load_model()`** (`:749`): thêm
   ```python
   elif ARGS.model == "index":
       m = IndexModel(); MODEL, backend, device = m, "index", m.device_name
   ```
9. **`synthesize()`** (`:928`): thêm nhánh dựng `run` lambda cho model mới (chọn kiểu `run(g)` hay `run(g, tag)` và xử lý tốc độ như F5 nếu cần).
10. **Nếu hỗ trợ giọng cá nhân**: chỉnh `clone()` (`:989`, lưu đặc trưng nếu có) và `transcribe_reference()` (`:1035`). Nếu có giọng dựng sẵn (preset) thì xử lý trong `do_GET /voices` (`:1092`).

### D. UI — `web/index.html`
11. **`#engineSelect`** (`:180`): thêm `<option value="index">IndexTTS · …</option>`.
12. **Mapping engine↔vmodel & body** (`~:618–624`, `:700–713`): để `engine='vieneu'` là nhãn "local" và `vmodel` chọn model con, hoặc thêm nhánh riêng. Body gửi lên dùng `engine: <id model>`.
13. Panel trạng thái/cài đặt, nút cài, kiểm tra `ready`, và (nếu có) menu thẻ cảm xúc tương tự `F5_STYLES`.

> **Ví dụ đã tích hợp: `voxcpm`** (VoxCPM2, OpenBMB, Apache-2.0) — model clone đa ngữ dùng ref audio + lời thoại, tái dùng toàn bộ nhánh ref/cảm xúc của F5. Xem `VoxCPMModel` trong `engine_server.py`, `case "voxcpm"` trong `doInstall`, và lớp UI "mô hình nâng cao" dùng chung (`isAdv()`/`advName()` trong `web/index.html`). Các chỗ phụ thuộc GPU runtime (tên tham số `generate()`, chuyển thiết bị, tên gói PyPI) cần kiểm chứng trên máy có GPU.

### E. Kiểm thử
14. Thêm/chỉnh test Go (`*_test.go`) và Python (`engine/*_test.py`).
    ```bash
    go test ./...
    GIONGAI_FAKE_ENGINE=1 python3 -m unittest discover -s engine -p '*_test.py'
    ```
    `GIONGAI_FAKE_ENGINE=1` dùng `FakeModel/FakeCloneModel` → test không cần tải model.

---

## 6. Biến môi trường hữu ích khi dev

| Biến | Tác dụng |
|------|----------|
| `GIONGAI_ENGINE_DIR` | Thư mục engine/model/venv (ưu tiên hơn cài đặt) |
| `GIONGAI_OUT` | Thư mục lưu audio xuất |
| `GIONGAI_FAKE_ENGINE=1` | Dùng model giả (không tải gì) để test nhanh |
| `GIONGAI_ENGINE_CPU=1` | Ép engine chạy CPU (truyền `--force-cpu`) |
| `GIONGAI_NO_BROWSER=1` | Không tự mở trình duyệt khi chạy |
| `GIONGAI_F5_PACKAGE` | Ghi đè package F5 (chỉ để kiểm thử) |

Chạy dev: `go run .` → mở `http://127.0.0.1:17860`. Sửa code/UI thì thoát app rồi chạy lại (UI nhúng qua `//go:embed`).
