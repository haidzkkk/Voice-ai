# UI web + engine chạy local

Mô hình: **UI host trên GitHub Pages**, user **cài & chạy engine** (binary) trên máy mình;
UI trong trình duyệt gọi API engine qua `http://127.0.0.1:17860`.

```
Trình duyệt → https://haidzkkk.github.io/Voice-ai/   (UI, tĩnh)
     │  fetch http://127.0.0.1:17860/api/…           (về máy user)
     ▼
Giọng AI Engine (binary cài local)  →  VoxCPM/VieNeu chạy trên máy user
```

Tạo giọng + lưu audio vẫn **100% trên máy user**. Tài khoản/quota/VIP vẫn qua Firebase như cũ.

## 1. Bật GitHub Pages
Repo → **Settings → Pages → Build and deployment → Source = GitHub Actions**.
Workflow `.github/workflows/pages.yml` tự deploy thư mục `web/` mỗi khi push `main`.
Sau lần chạy đầu, UI ở: **https://haidzkkk.github.io/Voice-ai/**

## 2. Cho phép đăng nhập Google từ domain Pages
Firebase Console → Authentication → Settings → **Authorized domains** → thêm
`haidzkkk.github.io`. (Nếu thiếu, popup đăng nhập sẽ báo lỗi unauthorized-domain.)

## 3. Phát hành engine (để nút "Tải engine" có file)
Tạo **GitHub Release** (tag vd `v1.2.0`) và đính kèm các file trong `dist/`
(`GiongAI-Windows-x64.zip`, `GiongAI-Windows-arm64.zip`). Nút "Tải engine" trong UI
trỏ tới `releases/latest` nên sẽ luôn lấy bản mới nhất.

## 4. CORS đã khóa sẵn
Binary chỉ nhận request kèm `Origin` là `https://haidzkkk.github.io` hoặc
`localhost`/`127.0.0.1` (xem `allowedOrigin` trong `main.go`). Đổi domain → sửa ở đó.

## Test nhanh trước khi deploy
- **Cách 1 (đơn giản):** chạy binary rồi mở `http://127.0.0.1:17860/` — binary tự phục vụ UI
  (cùng origin, không cần CORS). Giống hệt bản desktop cũ.
- **Cách 2 (giả lập web):** chạy binary; ở thư mục `web/` chạy `python3 -m http.server 8000`
  rồi mở `http://localhost:8000` → UI gọi engine qua CORS. (Mở bằng `file://` sẽ KHÔNG chạy.)

## Lưu ý trình duyệt (bẫy đã nói trước)
HTTPS gọi `http://127.0.0.1` bị kiểm soát bởi **Private/Local Network Access**. Binary đã trả
header `Access-Control-Allow-Private-Network: true`, nhưng **Chrome bản mới có thể hiện popup
xin phép truy cập mạng nội bộ** — cần test trên trình duyệt thật (Chrome/Edge/Safari/Firefox)
để xác nhận trải nghiệm. Nếu bị chặn gắt, phương án chắc ăn là dùng Cách 1 (mở qua 127.0.0.1).

## Dev: đổi địa chỉ engine
UI đọc `localStorage.engineBase` (mặc định `http://127.0.0.1:17860`). Đổi cổng/khác máy:
```js
localStorage.setItem('engineBase','http://127.0.0.1:5000')
```
