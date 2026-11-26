// POST /api/sepayWebhook — SePay gọi khi nhận được tiền → verify + cấp VIP (Admin SDK).
// Header: Authorization: Apikey <SEPAY_API_KEY>
const { admin, db } = require("../lib/admin");

const PLAN_DAYS = { month: 30 };

module.exports = async (req, res) => {
  try {
    if ((req.headers.authorization || "") !== "Apikey " + process.env.SEPAY_API_KEY) {
      res.status(401).json({ error: "unauthorized" }); return;
    }
    const b = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    if (b.transferType && b.transferType !== "in") { res.status(200).json({ success: true }); return; }

    const content = String(b.content || "").toUpperCase().replace(/\s+/g, "");
    const amount = Number(b.transferAmount || 0);
    const txId = String(b.id || b.referenceCode || "");
    const cm = content.match(/GAI[A-Z0-9]{6}/);
    if (!cm) { res.status(200).json({ success: true, note: "no_code" }); return; }

    const q = await db.collection("orders").where("code", "==", cm[0]).limit(1).get();
    if (q.empty) { res.status(200).json({ success: true, note: "order_not_found" }); return; }
    const orderRef = q.docs[0].ref;

    await db.runTransaction(async (tx) => {
      const o = await tx.get(orderRef);
      const od = o.data();
      if (od.status === "paid") return;     // idempotent
      if (amount < od.amount) return;       // chưa đủ tiền

      const userRef = db.collection("users").doc(od.uid);
      const u = await tx.get(userRef);
      const ud = u.exists ? (u.data() || {}) : {};

      if (od.plan === "lifetime") {
        tx.set(userRef, { isVip: true }, { merge: true });
      } else {
        const days = PLAN_DAYS[od.plan] || 30;
        let base = Date.now();
        if (ud.vipUntil && ud.vipUntil.toMillis && ud.vipUntil.toMillis() > base) base = ud.vipUntil.toMillis();
        tx.set(userRef, { vipUntil: admin.firestore.Timestamp.fromMillis(base + days * 86400000) }, { merge: true });
      }
      tx.set(orderRef, { status: "paid", txId, paidAmount: amount, paidAt: admin.firestore.FieldValue.serverTimestamp() }, { merge: true });
    });

    res.status(200).json({ success: true });
  } catch (e) {
    console.error("sepayWebhook", e);
    res.status(500).json({ error: "server" });
  }
};
