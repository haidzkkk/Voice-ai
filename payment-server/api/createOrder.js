// POST /api/createOrder  — client (đã đăng nhập) tạo đơn VIP, nhận VietQR.
// Header: Authorization: Bearer <Firebase ID token>. Body: { plan: "month"|"year"|"lifetime" }
const { admin, db } = require("../lib/admin");

// Giá (VND) — sửa tuỳ bạn. Số ngày ở webhook.
const PRICES = { month: 29000, year: 199000, lifetime: 499000, test: 2000 };

module.exports = async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Authorization, Content-Type");
  if (req.method === "OPTIONS") { res.status(204).end(); return; }
  if (req.method !== "POST") { res.status(405).json({ error: "method" }); return; }
  try {
    const m = (req.headers.authorization || "").match(/^Bearer (.+)$/);
    if (!m) { res.status(401).json({ error: "no_token" }); return; }
    const decoded = await admin.auth().verifyIdToken(m[1]);
    const uid = decoded.uid;

    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const plan = body.plan || "";
    if (!PRICES[plan]) { res.status(400).json({ error: "bad_plan" }); return; }

    const amount = PRICES[plan];
    const code = "GAI" + Math.random().toString(36).slice(2, 8).toUpperCase();
    const ref = db.collection("orders").doc();
    await ref.set({
      uid, plan, amount, code, status: "pending",
      createdAt: admin.firestore.FieldValue.serverTimestamp(),
    });

    const acc = process.env.BANK_ACCOUNT, bank = process.env.BANK_CODE;
    // VietinBank: SePay chỉ nhận biến động số dư khi nội dung CK bắt đầu bằng "SEVQR".
    // Mã đơn GAI... vẫn nằm trong nội dung nên webhook vẫn tìm thấy. Prefix này vô hại với bank khác.
    const payContent = "SEVQR " + code;
    const qrUrl = `https://qr.sepay.vn/img?acc=${encodeURIComponent(acc)}&bank=${encodeURIComponent(bank)}&amount=${amount}&des=${encodeURIComponent(payContent)}`;
    res.status(200).json({ orderId: ref.id, code, payContent, amount, qrUrl, account: acc, bank, bankName: process.env.BANK_NAME });
  } catch (e) {
    console.error("createOrder", e);
    res.status(500).json({ error: "server: " + String((e && e.message) || e) });
  }
};
