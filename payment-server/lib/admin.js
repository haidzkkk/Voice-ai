// Khởi tạo Firebase Admin một lần, dùng service account từ biến môi trường Vercel.
// FIREBASE_SERVICE_ACCOUNT = nội dung JSON của service account (dán nguyên vào env trên Vercel).
const admin = require("firebase-admin");

if (!admin.apps.length) {
  const svc = JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT || "{}");
  admin.initializeApp({ credential: admin.credential.cert(svc) });
}

module.exports = { admin, db: admin.firestore() };
