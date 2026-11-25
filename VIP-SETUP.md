# Nâng cấp VIP qua SePay (VietQR) — backend free (Render hoặc Vercel, không cần thẻ)

Giữ Firebase ở gói **Spark (free)** cho Auth + Firestore. Hai endpoint thanh toán chạy trên
**Render** (web service free) hoặc **Vercel** (serverless free), dùng **Firebase Admin SDK +
service account** để cấp VIP. Web UI vẫn ở GitHub Pages như hiện tại, chỉ gọi sang backend này.

Luồng: app gọi `createOrder` → hiện VietQR → user chuyển khoản (nội dung = mã đơn)
→ SePay gọi `sepayWebhook` → cấp VIP → app mở khoá realtime.

Giá/gói sửa trong `payment-server/api/createOrder.js` (`PRICES`) và `sepayWebhook.js` (`PLAN_DAYS`).
Mặc định: tháng 29.000đ/30 ngày, năm 199.000đ/365 ngày, vĩnh viễn 499.000đ.

## 1. Lấy service account (để server cấp VIP)
Firebase Console → ⚙ Project settings → **Service accounts** → **Generate new private key**
→ tải file JSON về. (Đây là khoá quyền admin — chỉ dán vào env Vercel, KHÔNG commit, KHÔNG đưa client.)

## 2. SePay
1. Đăng ký https://sepay.vn, **liên kết tài khoản ngân hàng** nhận tiền.
2. Mục Webhooks: tạo webhook, tự đặt một **API Key** (chuỗi bất kỳ, nhớ lại). URL điền ở bước 4.

## 3. Publish Firestore Rules (Spark vẫn làm được)
```
npm install -g firebase-tools   # nếu chưa có
firebase login
firebase deploy --only firestore:rules
```
(Hoặc dán nội dung `firestore.rules` vào Console → Firestore → Rules → Publish.)

## 4. Deploy backend `payment-server/`
Cùng một thư mục chạy được trên cả hai (cùng bộ Environment Variables):
- `FIREBASE_SERVICE_ACCOUNT` = dán **nguyên nội dung JSON** service account ở bước 1
- `SEPAY_API_KEY` = API key đã đặt ở SePay (bước 2)
- `BANK_ACCOUNT` = số tài khoản nhận tiền
- `BANK_CODE` = mã ngân hàng VietQR (vd MB, VCB, ACB, TCB)
- `BANK_NAME` = tên chủ tài khoản (hiển thị)

### Cách A — Render (web service thường trực, dùng `server.js`)
1. https://render.com → **New** → **Web Service** → kết nối repo GitHub.
2. **Root Directory** = `payment-server`
   **Build Command** = `npm install`
   **Start Command** = `npm start`   (Render tự cấp `PORT`)
3. Thêm 5 Environment Variables ở trên → **Create Web Service**.
4. Được domain dạng `https://<tên>.onrender.com`.
   ⚠️ **Gói free "ngủ" sau ~15 phút không dùng** → lần gọi đầu chờ ~30–60s (QR hiện chậm; webhook SePay có thể trượt lần đầu nhưng SePay **tự gửi lại** nên VIP vẫn được cấp, chỉ trễ). Muốn luôn tỉnh: dùng cron-job.org ping `https://<tên>.onrender.com/` mỗi ~10 phút.

### Cách B — Vercel (serverless, dùng `api/*.js`)
1. https://vercel.com (không cần thẻ) → **Add New Project** → import repo.
2. **Root Directory** = `payment-server`. Thêm 5 env ở trên → Deploy.
3. Được domain dạng `https://<tên>.vercel.app`. (Không bị "ngủ" như Render free.)

## 5. Nối vào app
Gọi `<domain>` là domain ở bước 4 (`.onrender.com` hoặc `.vercel.app`):
- `createOrder`:  `<domain>/api/createOrder`
  → dán vào `web/index.html` biến **`CREATE_ORDER_URL`** (rồi commit để GitHub Pages deploy lại).
- `sepayWebhook`: `<domain>/api/sepayWebhook`
  → dán vào **Webhook URL** trên SePay (kèm đúng API Key bước 2).

## 6. Test
1. Mở app → đăng nhập → **Nâng cấp VIP** → chọn gói → hiện VietQR.
2. Chuyển đúng **số tiền** + **nội dung** (`GAI…`). Nên test số tiền nhỏ trước.
3. Vài giây sau: `users/{uid}` được set `isVip:true` (vĩnh viễn) hoặc `vipUntil` (tháng/năm);
   thẻ user hiện VIP, hết giới hạn; đơn trong `orders/` thành `status:"paid"`.

## Ghi chú
- VIP chỉ cấp bởi server (service account) — client/Rules không tự set được.
- `FIREBASE_SERVICE_ACCOUNT` là khoá rất mạnh: chỉ để trong env Vercel.
- Webhook idempotent; gói tháng/năm cộng dồn nếu còn hạn; vĩnh viễn = `isVip:true`.
- Đổi giá: sửa `PRICES`/`PLAN_DAYS` rồi deploy lại Vercel.
