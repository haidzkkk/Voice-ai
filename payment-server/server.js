// Web service thường trực cho Render (hoặc host Node bất kỳ).
// Bọc 2 hàm serverless (api/*.js) thành route Express — dùng lại y nguyên logic.
// Env cần có: FIREBASE_SERVICE_ACCOUNT, SEPAY_API_KEY, BANK_ACCOUNT, BANK_CODE, BANK_NAME.
const express = require("express");
const createOrder = require("./api/createOrder");
const sepayWebhook = require("./api/sepayWebhook");

const app = express();
app.use(express.json({ limit: "1mb" })); // parse JSON body → req.body (client & SePay đều gửi JSON)

// Health check: Render ping đường này; cũng tiện để giữ service khỏi "ngủ".
app.get("/", (_req, res) => res.json({ ok: true, service: "giongai-payment" }));

app.all("/api/createOrder", (req, res) => createOrder(req, res));
app.all("/api/sepayWebhook", (req, res) => sepayWebhook(req, res));

const port = process.env.PORT || 3000;
app.listen(port, () => console.log("payment-server listening on", port));
