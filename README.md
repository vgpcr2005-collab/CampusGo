# CampusGo

CampusGo is a focused, one-campus MVP for student accounts, academic-supply orders, available-student delivery, delivery status updates, and transparent order fees.

## Architecture

- **Frontend:** React, TypeScript, and Vite (`frontend/`).
- **Backend:** Flask JSON API (`backend/app.py`).
- **Database:** SQLite for a self-contained local pilot. It stores users, products, orders, line items, status history, payment records, Route transfers/refunds, webhook event IDs, and payment allocations. Monetary amounts are integer paise.
- **Authentication:** Server-side Flask sessions with HttpOnly/SameSite cookies, password hashing, and a CSRF token for authenticated mutations.
- **Order assignment:** SQLite `BEGIN IMMEDIATE` plus a conditional update ensures only one available student can accept an open order.

```text
campusgo/
├── backend/
│   ├── app.py
│   ├── test_app.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   ├── package.json
│   └── vite.config.ts
└── README.md
```

## Run locally (Windows PowerShell)

Open two terminals in the project directory.

### 1. Start the API

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item backend\.env.example .env
# Edit .env and set FLASK_SECRET_KEY, RAZORPAY_KEY_ID, and RAZORPAY_KEY_SECRET.
# Use a Razorpay Test Mode key ID (rzp_test_...) and its matching test secret.
# Enable QR API access for the merchant account, then set RAZORPAY_QR_ENABLED=true.
python backend\app.py
```

The backend loads the root `.env` file. The API listens on `http://127.0.0.1:5000`. SQLite tables and additive columns are created automatically at `backend\instance\campusgo.sqlite3` when the app starts. Legacy unpaid COD and simulated-payment orders are cancelled; old simulated payment records are never treated as captured. New order writes with any non-online payment method are blocked.

### 2. Start the web app

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal (normally `http://127.0.0.1:5173`). Vite proxies `/api` requests to Flask.

To serve a production frontend through Flask, build it with `npm run build` in `frontend`, then run the backend. The Flask app serves `frontend\dist`.

## Verify

Run backend workflow tests:

```powershell
$env:PYTHONPATH = "backend"
python -m unittest discover -s backend -p "test_*.py" -v
```

Run the frontend type-check and production build:

```powershell
cd frontend
npm run build
```

## MVP workflow and money flow

1. A student creates one account and can order or opt into delivery mode.
2. The customer chooses catalog items and one of the predefined drop-off points (BTech Block, Law Block, or Library). The server looks up item prices and calculates item subtotal, the highest applicable single delivery fee, platform fee, and total.
3. **Online payment only:** the server creates a single-use Razorpay QR for its recalculated INR total. An order remains private and unavailable to runners until the backend confirms the payment is captured through Razorpay's API or signed webhook.
4. The order stores item subtotal, delivery fee, platform fee, payment amount/status/signature verification, gateway fees/tax, and separate recipient allocation and transfer statuses. Runner assignment is conditional and is allowed only after payment capture.
5. After pickup and delivery status updates, the customer confirms completion. Confirmation does not mark a transfer as settled; only a verified Razorpay transfer webhook changes a transfer to `processed`.

Catalog prices and fee rules are stored with each product in SQLite. Bluebook basic costs ₹10 (₹12 delivery, ₹3 platform fee); premium costs ₹30 (₹15 delivery, ₹3 platform fee). Xerox's shop base cost is ₹0.50 per billable page, separate from its configurable customer selling price (initially ₹3 per page) and CampusGo commission (₹0.50 per billable page). The customer total uses selling price × billable pages, plus delivery and the separately itemized platform commission; the shop allocation is the full selling subtotal, the delivery partner allocation is the delivery fee, and the platform allocation is the commission. Shop base cost is recorded separately for margin reporting and is not added to the customer total or deducted a second time from the shop allocation. Black-and-white print costs ₹3 per page, and color printing retains its configured ₹10 per-page price. Printing delivery is ₹5 per order with a ₹0.50 platform fee per billable page. Ordinary stationery uses its configured price, a single ₹5 delivery fee, and one ₹2 platform fee per order. A mixed cart pays the highest applicable delivery fee once.

All monetary calculations use integer paise. Printing billable pages equal page count multiplied by copies. The backend independently calculates and snapshots the selling price, shop unit cost, subtotal, delivery fee, platform fee, and customer total when the order is created; client-supplied prices are ignored. The frontend refreshes catalog pricing before submission and the backend rejects a checkout if the displayed total no longer matches the current server-calculated total.

### Shop owner Xerox price management

The shop owner can read and update the Xerox customer selling price through the admin-token-protected endpoint. Configure a strong `CAMPUSGO_ADMIN_TOKEN` in the root `.env`; send that token in the `X-Admin-Token` header. Prices are whole paise values (`300` = ₹3.00/page). The shop base cost remains ₹0.50/page and the current CampusGo commission remains ₹0.50/page.

```powershell
$headers = @{ "X-Admin-Token" = "<CAMPUSGO_ADMIN_TOKEN>" }
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:5000/api/admin/pricing/xerox -Headers $headers
$body = @{ selling_price_paise = 300 } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:5000/api/admin/pricing/xerox -Headers $headers -ContentType "application/json" -Body $body
```

The admin endpoint returns base cost, customer selling price, per-page CampusGo commission, and delivery fee as separate values. Order item cost and selling-price snapshots are available to the admin reconciliation endpoint; the public catalog only exposes customer-facing prices.

## Printing document uploads

Select a Xerox or printout item to show the document upload controls in checkout. Upload a PDF up to 20 MB; the backend validates the PDF structure, detects its page count, and stores the file in `backend\instance\private_documents` under a random server-generated name. The original filename is metadata only; document keys and public file URLs are never returned.

Customers can choose all pages or a non-repeating page range such as `1-3,5`, copies, and single- or double-sided printing. The server validates the selected range against the uploaded PDF's actual page count and calculates billable pages and fees before attaching the document to the order. The order and document are linked in SQLite. An authenticated customer can download only their own order document through a private, no-store endpoint; delivery students do not receive access because the delivery workflow only requires collecting the finished printouts. This MVP has no separate staff role or staff document console.

Unattached uploads can be replaced or removed by their owner. Keep the private document directory out of static/web server roots and define an operational retention and backup policy before using real student documents.

## Razorpay payments

Customer checkout uses Razorpay Standard Checkout. The backend creates an order for the server-calculated total; the browser opens Razorpay's secure payment dialog for UPI, cards, net banking, and wallets. The order remains unavailable to runners until the backend verifies the payment signature and confirms the captured payment with Razorpay. Failed, cancelled, or unverified payments never release an order.

| Environment variable | Purpose |
| --- | --- |
| `FLASK_SECRET_KEY` | Random, private Flask session-signing key. |
| `COOKIE_SECURE` | Set to `true` for HTTPS deployments such as Render. |
| `RAZORPAY_MODE` | `test` by default; `live` requires a matching `rzp_live_...` key ID. |
| `RAZORPAY_KEY_ID` | Razorpay Dashboard API key ID; `rzp_test_...` for test mode. This public key ID is returned to Checkout. |
| `RAZORPAY_KEY_SECRET` | Private Razorpay API secret; server only. Never put it in Vite variables or frontend code. |
| `RAZORPAY_WEBHOOK_SECRET` | Secret configured for the Razorpay webhook endpoint. |
| `RAZORPAY_ROUTE_ENABLED` | `false` by default. Set true only after Route capability and linked accounts are approved and configured. |
| `RAZORPAY_SHOP_LINKED_ACCOUNT_ID` | Approved shop Route linked-account ID (`acc_...`). |
| `CAMPUSGO_ADMIN_TOKEN` | Private token for read-only reconciliation and mapping runner IDs to approved Route accounts. |

For a split Render deployment, set `VITE_API_BASE_URL` on the frontend service to the backend's HTTPS origin (for example, `https://campusgo-8hkr.onrender.com`) and redeploy the frontend. Set Razorpay keys, `RAZORPAY_MODE`, `FLASK_SECRET_KEY`, and `COOKIE_SECURE=true` on the backend service. Generate a strong, unique Flask secret and keep `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` exclusively on the backend. The backend CORS allowlist must match the deployed frontend origin. The local `.env` is git-ignored; never commit secrets.

The older UPI QR endpoints remain available for compatibility but are not used by the customer checkout. They require Razorpay QR Code API access and `RAZORPAY_QR_ENABLED=true`.

### Webhooks and verification

Configure these webhook events in the Razorpay Dashboard and set the matching `RAZORPAY_WEBHOOK_SECRET`: `payment.authorized`, `payment.captured`, `payment.failed`, `refund.created`, `refund.processed`, `refund.failed`, `refund.speed_changed`, `transfer.processed`, `transfer.failed`, and `transfer.reversed`. Point the webhook to `https://<your-backend-host>/api/webhooks/razorpay`. Razorpay requires a publicly reachable endpoint for webhook delivery; localhost itself is not a valid webhook destination. The backend verifies the HMAC over the raw body and deduplicates by event ID and payload digest. The authenticated `POST /api/orders/<id>/verify-payment` endpoint checks the signature and confirms payment details with Razorpay's API; browser/client confirmation alone does not mark an order paid.

In development, configure test mode and use Razorpay's documented test payment details. The local automated tests use an isolated fake gateway transport and do not make real payments. Test the customer payment flow in the Razorpay Test Mode Dashboard before enabling live keys.

### Razorpay Route eligibility and transfer configuration

Payment collection does not itself split a payment. This code only initiates Route transfers after a captured payment and only to exact linked-account IDs already supplied through server configuration / an admin mapping. The intended allocation remains item subtotal to the shop, delivery fee to the assigned runner, and platform fee retained by CampusGo. Gateway fees and tax are recorded separately and reduce the amount available for transfers; the app will leave a transfer pending/unavailable if the recipient amounts do not fit within captured proceeds after fees. Transfer statuses are changed to processed/failed only from verified Razorpay events.

Route must be approved and enabled for the merchant account; the official requirements include business turnover/compliance checks and payer-payee transparency review. Linked accounts must be created and onboarded through Razorpay Dashboard/API and pass the documented 24-hour cooling period before transfers. The user-facing app intentionally does not collect arbitrary bank details or create linked accounts. Enable Route only after Razorpay confirms the feature for the account and all recipient linked accounts are approved.

Set `RAZORPAY_ROUTE_ENABLED=true`, the shop's approved ID in `RAZORPAY_SHOP_LINKED_ACCOUNT_ID`, and map runner accounts with the admin-only endpoint:

```powershell
$headers = @{ "X-Admin-Token" = "<CAMPUSGO_ADMIN_TOKEN>" }
$body = @{ runner_user_id = 2; linked_account_id = "acc_approved_runner_id" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/api/admin/route-accounts -Headers $headers -ContentType "application/json" -Body $body
```

Use `GET /api/admin/reconciliation` with the same `X-Admin-Token` header for payment, allocation, gateway fee, refund, and transfer-status visibility. Protect this endpoint with a strong secret and HTTPS in deployments. The endpoint does not claim settlement succeeded until Razorpay's `transfer.processed` webhook is verified.

### Idempotency and retries

Every checkout request includes a per-attempt idempotency key and request fingerprint. A retry with the same key and identical order payload reuses the saved CampusGo order and Razorpay order; reusing the key with a different payload is rejected. Cancelled or failed payment attempts remain unpaid and can be retried from Order activity. Duplicate webhook payloads, including replays with a changed event-ID header, are ignored. Payment failure/cancellation never makes an order available to runners.

## Pilot boundaries

- One configured campus, one demo campus shop, and a predefined set of drop-off locations.
- A newly registered student account can use delivery mode; no college email verification or administrator verification is implemented.
- Sample catalog and fee settings are local seed/configuration data.
- The database is local SQLite; deploy with a managed database and production migrations before a multi-user rollout.
- No live maps, chat, real-time push, reviews, or full production payout/reconciliation operations console is included in this first feature slice.
- Set a unique secret key, HTTPS, secure cookies, rate limiting, college verification, privacy/retention rules, and operational support before a public launch.
