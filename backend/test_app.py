from io import BytesIO
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path

import app as campusgo
from pypdf import PdfWriter


class CampusGoApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_database = campusgo.DATABASE
        self.previous_private_documents = campusgo.PRIVATE_DOCUMENTS
        self.previous_gateway_request = campusgo.razorpay_request
        self.previous_environment = {
            key: os.environ.get(key)
            for key in (
                "RAZORPAY_MODE",
                "RAZORPAY_KEY_ID",
                "RAZORPAY_KEY_SECRET",
                "RAZORPAY_WEBHOOK_SECRET",
                "RAZORPAY_QR_ENABLED",
                "RAZORPAY_ROUTE_ENABLED",
                "RAZORPAY_SHOP_LINKED_ACCOUNT_ID",
                "CAMPUSGO_ADMIN_TOKEN",
            )
        }
        os.environ.update({
            "RAZORPAY_MODE": "test",
            "RAZORPAY_KEY_ID": "rzp_test_fixture",
            "RAZORPAY_KEY_SECRET": "test-api-secret",
            "RAZORPAY_WEBHOOK_SECRET": "test-webhook-secret",
            "RAZORPAY_QR_ENABLED": "true",
            "RAZORPAY_ROUTE_ENABLED": "false",
            "RAZORPAY_SHOP_LINKED_ACCOUNT_ID": "",
            "CAMPUSGO_ADMIN_TOKEN": "test-admin-token",
        })
        self.gateway_orders = {}
        self.gateway_payments = {}
        self.gateway_qr_codes = {}
        self.gateway_qr_payments = {}
        self.gateway_order_counter = 0

        def fake_gateway_request(method, path, payload=None):
            if method == "POST" and path == "/payments/qr_codes":
                qr_id = f"qr_test_{len(self.gateway_qr_codes) + 1}"
                self.gateway_qr_codes[qr_id] = dict(payload)
                return {
                    "id": qr_id,
                    "image_url": f"https://rzp.io/i/{qr_id}",
                    "payment_amount": payload["payment_amount"],
                    "fixed_amount": payload["fixed_amount"],
                    "usage": payload["usage"],
                    "status": "active",
                    "close_by": payload["close_by"],
                }
            if method == "GET" and path.startswith("/payments/qr_codes/"):
                qr_id = path.split("/")[3]
                return {"items": self.gateway_qr_payments.get(qr_id, [])}
            if method == "POST" and path == "/orders":
                self.gateway_order_counter += 1
                gateway_id = f"order_test_{self.gateway_order_counter}"
                self.gateway_orders[gateway_id] = dict(payload)
                return {
                    "id": gateway_id,
                    "amount": payload["amount"],
                    "currency": payload["currency"],
                    "status": "created",
                }
            if method == "GET" and path.startswith("/payments/"):
                payment_id = path.rsplit("/", 1)[-1]
                if payment_id not in self.gateway_payments:
                    raise campusgo.RazorpayError("Payment not found.")
                return self.gateway_payments[payment_id]
            if method == "POST" and path.endswith("/transfers"):
                self.gateway_transfer_counter = getattr(self, "gateway_transfer_counter", 0) + 1
                return {
                    "items": [
                        {
                            "id": f"tr_test_{self.gateway_transfer_counter}_{index}",
                            "account": item["account"],
                            "amount": item["amount"],
                            "status": "created",
                        }
                        for index, item in enumerate(payload["transfers"], start=1)
                    ]
                }
            raise AssertionError(f"Unexpected gateway request: {method} {path}")

        campusgo.razorpay_request = fake_gateway_request
        self.request_counter = 0
        campusgo.DATABASE = Path(self.temp_dir.name) / "test.sqlite3"
        campusgo.PRIVATE_DOCUMENTS = Path(self.temp_dir.name) / "private_documents"
        campusgo.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        campusgo.init_db()
        self.customer = campusgo.app.test_client()
        self.customer_csrf = self.register(self.customer, "Customer")

    def tearDown(self):
        campusgo.DATABASE = self.previous_database
        campusgo.PRIVATE_DOCUMENTS = self.previous_private_documents
        campusgo.razorpay_request = self.previous_gateway_request
        for key, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def register(self, client, name):
        response = client.post(
            "/api/auth/register",
            json={
                "name": name,
                "email": f"{name.lower()}@campus.test",
                "password": "test-password-123",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["csrf_token"]

    def post(self, client, csrf, route, payload=None):
        headers = {"X-CSRF-Token": csrf}
        if route == "/api/orders":
            self.request_counter += 1
            headers["X-Idempotency-Key"] = f"test-order-key-{self.request_counter}"
        return client.post(
            route,
            json=payload or {},
            headers=headers,
        )

    def product(self, name):
        products = self.customer.get("/api/catalog").get_json()["products"]
        return next(product for product in products if product["name"] == name)

    def create_order(self, items, destination="Library"):
        normalized_items = []
        page_total = 0
        needs_document = False
        for item in items:
            normalized = dict(item)
            product = next(
                product for product in self.customer.get("/api/catalog").get_json()["products"]
                if product["id"] == item["product_id"]
            )
            if product["pricing_type"] == "page":
                needs_document = True
                page_total = max(page_total, int(item.get("page_count", 5)))
                normalized.pop("page_count", None)
                normalized.setdefault("page_range", "all")
            normalized_items.append(normalized)
        document_id = None
        if needs_document:
            document = self.upload_pdf(content=self.pdf_bytes(page_total)).get_json()["document"]
            document_id = document["id"]
        response = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": normalized_items,
                **({"document_id": document_id} if document_id else {}),
                "destination": destination,
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["order"]

    def create_qr_order(self, items, idempotency_key="qr-order-test-key"):
        response = self.customer.post(
            "/api/orders",
            json={
                "items": items,
                "destination": "Library",
                "payment_flow": "qr",
            },
            headers={
                "X-CSRF-Token": self.customer_csrf,
                "X-Idempotency-Key": idempotency_key,
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def capture_order(self, order, client=None, csrf=None, payment_status="captured"):
        connection = campusgo.connect_db()
        payment_record = connection.execute(
            "SELECT gateway_order_id, amount_paise FROM payment_records WHERE order_id = ?",
            (order["id"],),
        ).fetchone()
        connection.close()
        payment_id = f"pay_test_{order['id']}"
        entity = {
            "id": payment_id,
            "order_id": payment_record["gateway_order_id"],
            "amount": payment_record["amount_paise"],
            "currency": "INR",
            "status": payment_status,
            "fee": 100,
            "tax": 18,
        }
        self.gateway_payments[payment_id] = entity
        signature = hmac.new(
            b"test-api-secret",
            f"{entity['order_id']}|{payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        response = self.post(
            client or self.customer,
            csrf or self.customer_csrf,
            f"/api/orders/{order['id']}/verify-payment",
            {
                "razorpay_order_id": entity["order_id"],
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            },
        )
        return response

    def pdf_bytes(self, page_count):
        writer = PdfWriter()
        for _ in range(page_count):
            writer.add_blank_page(width=72, height=72)
        with BytesIO() as document:
            writer.write(document)
            return document.getvalue()

    def upload_pdf(self, filename="print.pdf", content=None, client=None, csrf=None):
        response = (client or self.customer).post(
            "/api/documents",
            data={"document": (BytesIO(content or self.pdf_bytes(5)), filename)},
            headers={"X-CSRF-Token": csrf or self.customer_csrf},
            content_type="multipart/form-data",
        )
        return response

    def test_cross_origin_checkout_preflight_allows_idempotency_header(self):
        response = self.customer.options(
            "/api/orders",
            headers={
                "Origin": "https://campusgo-frontend-xpwd.onrender.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "content-type,x-csrf-token,x-idempotency-key"
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "https://campusgo-frontend-xpwd.onrender.com",
        )
        self.assertEqual(response.headers["Access-Control-Allow-Credentials"], "true")
        allowed_headers = response.headers["Access-Control-Allow-Headers"].lower()
        self.assertIn("x-idempotency-key", allowed_headers)

    def test_bluebook_variants_have_distinct_prices_and_delivery_fees(self):
        basic = self.product("Bluebook — Basic")
        premium = self.product("Bluebook — Premium")
        basic_order = self.create_order([{"product_id": basic["id"], "quantity": 1}])
        premium_order = self.create_order([{"product_id": premium["id"], "quantity": 1}])

        self.assertEqual(
            (basic_order["subtotal_paise"], basic_order["delivery_fee_paise"],
             basic_order["platform_fee_paise"], basic_order["total_paise"]),
            (1000, 1200, 300, 2500),
        )
        self.assertEqual(
            (premium_order["subtotal_paise"], premium_order["delivery_fee_paise"],
             premium_order["platform_fee_paise"], premium_order["total_paise"]),
            (3000, 1500, 300, 4800),
        )

    def test_xerox_uses_billable_pages_for_item_and_platform_fees(self):
        xerox = self.product("Xerox copies (B&W)")
        self.assertEqual(xerox["price_paise"], 300)
        one_page = self.create_order(
            [{"product_id": xerox["id"], "page_count": 1, "copies": 1}]
        )
        ten_pages = self.create_order(
            [{"product_id": xerox["id"], "page_count": 10, "copies": 1}]
        )
        twenty_five_pages = self.create_order(
            [{"product_id": xerox["id"], "page_count": 25, "copies": 1}]
        )
        self.assertEqual(
            (
                one_page["subtotal_paise"],
                one_page["delivery_fee_paise"],
                one_page["platform_fee_paise"],
                one_page["total_paise"],
            ),
            (300, 500, 50, 850),
        )
        self.assertEqual(
            (
                ten_pages["subtotal_paise"],
                ten_pages["delivery_fee_paise"],
                ten_pages["platform_fee_paise"],
                ten_pages["total_paise"],
            ),
            (3000, 500, 500, 4000),
        )
        self.assertEqual(
            (
                twenty_five_pages["subtotal_paise"],
                twenty_five_pages["delivery_fee_paise"],
                twenty_five_pages["platform_fee_paise"],
                twenty_five_pages["total_paise"],
            ),
            (7500, 500, 1250, 9250),
        )
        self.assertEqual(ten_pages["items"][0]["billable_pages"], 10)
        with campusgo.app.app_context():
            saved = campusgo.get_db().execute(
                """SELECT unit_price_paise, unit_cost_paise
                   FROM order_items WHERE order_id = ?""",
                (ten_pages["id"],),
            ).fetchone()
        self.assertEqual((saved["unit_price_paise"], saved["unit_cost_paise"]), (300, 50))

        paid = self.capture_order(ten_pages)
        self.assertEqual(paid.status_code, 200, paid.get_json())
        self.assertEqual(
            [(allocation["recipient_type"], allocation["amount_paise"])
             for allocation in paid.get_json()["order"]["allocations"]],
            [("shop", 3000), ("runner", 500), ("platform", 500)],
        )
        reconciliation = self.customer.get(
            "/api/admin/reconciliation",
            headers={"X-Admin-Token": "test-admin-token"},
        )
        self.assertEqual(reconciliation.status_code, 200, reconciliation.get_json())
        reconciled = next(
            item for item in reconciliation.get_json()["payments"]
            if item["order_id"] == ten_pages["id"]
        )
        self.assertEqual(
            (
                reconciled["items"][0]["unit_price_paise"],
                reconciled["items"][0]["unit_cost_paise"],
                reconciled["items"][0]["selling_subtotal_paise"],
                reconciled["items"][0]["shop_cost_total_paise"],
            ),
            (300, 50, 3000, 500),
        )

    def test_xerox_selling_price_is_admin_configurable_and_survives_restart(self):
        endpoint = "/api/admin/pricing/xerox"
        headers = {"X-Admin-Token": "test-admin-token"}
        unauthorized = self.customer.get(endpoint)
        self.assertEqual(unauthorized.status_code, 401)

        initial = self.customer.get(endpoint, headers=headers)
        self.assertEqual(initial.status_code, 200, initial.get_json())
        self.assertEqual(
            (
                initial.get_json()["base_cost_paise"],
                initial.get_json()["selling_price_paise"],
                initial.get_json()["platform_commission_per_page_paise"],
            ),
            (50, 300, 50),
        )
        invalid = self.customer.put(
            endpoint,
            json={"selling_price_paise": True},
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 400)
        updated = self.customer.put(
            endpoint,
            json={"selling_price_paise": 425},
            headers=headers,
        )
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertEqual(updated.get_json()["selling_price_paise"], 425)
        self.assertEqual(updated.get_json()["base_cost_paise"], 50)

        campusgo.init_db()
        xerox = self.product("Xerox copies (B&W)")
        order = self.create_order(
            [{"product_id": xerox["id"], "page_count": 10, "copies": 1}]
        )
        self.assertEqual(
            (
                order["subtotal_paise"],
                order["delivery_fee_paise"],
                order["platform_fee_paise"],
                order["total_paise"],
            ),
            (4250, 500, 500, 5250),
        )

    def test_order_rejects_stale_displayed_total(self):
        xerox = self.product("Xerox copies (B&W)")
        uploaded = self.upload_pdf(content=self.pdf_bytes(10)).get_json()["document"]
        response = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": [{"product_id": xerox["id"], "copies": 1, "page_range": "all"}],
                "document_id": uploaded["id"],
                "destination": "Library",
                "expected_total_paise": 1500,
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["pricing_changed"])
        with campusgo.app.app_context():
            order_count = campusgo.get_db().execute(
                "SELECT COUNT(*) AS count FROM orders"
            ).fetchone()["count"]
        self.assertEqual(order_count, 0)

    def test_print_charges_pages_times_copies(self):
        printing = self.product("Printout (B&W)")
        order = self.create_order(
            [{"product_id": printing["id"], "page_count": 10, "copies": 2}]
        )
        self.assertEqual(
            (order["subtotal_paise"], order["delivery_fee_paise"],
             order["platform_fee_paise"], order["total_paise"]),
            (6000, 500, 1000, 7500),
        )
        self.assertEqual(order["items"][0]["billable_pages"], 20)

    def test_stationery_shares_one_delivery_fee_and_one_platform_fee(self):
        pen = self.product("Pen")
        record_sheets = self.product("Record sheets")
        order = self.create_order(
            [
                {"product_id": pen["id"], "quantity": 2},
                {"product_id": record_sheets["id"], "quantity": 1},
            ]
        )
        self.assertEqual(
            (order["subtotal_paise"], order["delivery_fee_paise"],
             order["platform_fee_paise"], order["total_paise"]),
            (4500, 500, 200, 5200),
        )
        one_pen = self.create_order([{"product_id": pen["id"], "quantity": 1}])
        self.assertEqual(
            (one_pen["subtotal_paise"], one_pen["delivery_fee_paise"],
             one_pen["platform_fee_paise"], one_pen["total_paise"]),
            (1000, 500, 200, 1700),
        )

    def test_file_product_is_retired_from_catalog(self):
        connection = campusgo.connect_db()
        connection.execute(
            """INSERT INTO products
               (name, category, description, price_paise, pricing_type, delivery_fee_paise,
                platform_fee_paise, platform_fee_per_page_paise, unit_cost_paise, active)
               VALUES ('File', 'Stationery', 'Academic document file', 1500, 'stationery',
                       500, 200, 0, 0, 1)"""
        )
        connection.execute(
            "DELETE FROM system_metadata WHERE key = 'file_product_retired'"
        )
        connection.commit()
        connection.close()

        campusgo.init_db()

        catalog = self.customer.get("/api/catalog").get_json()["products"]
        self.assertNotIn("File", [product["name"] for product in catalog])
        connection = campusgo.connect_db()
        file_product = connection.execute(
            "SELECT active FROM products WHERE name = 'File'"
        ).fetchone()
        connection.close()
        self.assertEqual(file_product["active"], 0)

    def test_mixed_cart_uses_highest_delivery_fee_once(self):
        basic = self.product("Bluebook — Basic")
        premium = self.product("Bluebook — Premium")
        pen = self.product("Pen")
        order = self.create_order(
            [
                {"product_id": basic["id"], "quantity": 1},
                {"product_id": premium["id"], "quantity": 1},
                {"product_id": pen["id"], "quantity": 1},
            ],
            destination="BTech Block",
        )
        self.assertEqual(order["delivery_fee_paise"], 1500)
        self.assertEqual(order["platform_fee_paise"], 800)
        self.assertEqual(order["total_paise"], 7300)

    def test_order_qr_has_exact_fixed_amount_and_api_verification_releases_order(self):
        pen = self.product("Pen")
        result = self.create_qr_order(
            [{"product_id": pen["id"], "quantity": 1}],
        )
        order = result["order"]
        payment_qr = result["payment_qr"]
        qr_request = self.gateway_qr_codes[payment_qr["qr_code_id"]]

        self.assertEqual(qr_request["type"], "upi_qr")
        self.assertEqual(qr_request["usage"], "single_use")
        self.assertTrue(qr_request["fixed_amount"])
        self.assertEqual(qr_request["payment_amount"], order["total_paise"])
        self.assertEqual(order["status"], "PAYMENT_PENDING")
        self.assertEqual(order["qr_payment"]["qr_code_id"], payment_qr["qr_code_id"])

        payment_id = "pay_test_qr_capture"
        payment_entity = {
            "id": payment_id,
            "qr_code_id": payment_qr["qr_code_id"],
            "order_id": None,
            "amount": order["total_paise"],
            "currency": "INR",
            "status": "captured",
            "fee": 100,
            "tax": 18,
        }
        self.gateway_qr_payments[payment_qr["qr_code_id"]] = [
            {
                "id": payment_id,
                "amount": order["total_paise"],
                "currency": "INR",
                "status": "captured",
            }
        ]
        self.gateway_payments[payment_id] = payment_entity
        verified = self.post(
            self.customer,
            self.customer_csrf,
            f"/api/orders/{order['id']}/verify-qr-payment",
        )
        self.assertEqual(verified.status_code, 200, verified.get_json())
        self.assertEqual(verified.get_json()["payment_status"], "captured")
        self.assertEqual(verified.get_json()["order"]["status"], "PLACED")

    def test_captured_qr_webhook_verifies_and_releases_order(self):
        pen = self.product("Pen")
        result = self.create_qr_order(
            [{"product_id": pen["id"], "quantity": 1}],
            idempotency_key="qr-webhook-order-key",
        )
        order = result["order"]
        payment_qr = result["payment_qr"]
        entity = {
            "id": "pay_test_qr_webhook",
            "qr_code_id": payment_qr["qr_code_id"],
            "order_id": None,
            "amount": order["total_paise"],
            "currency": "INR",
            "status": "captured",
            "fee": 0,
            "tax": 0,
        }
        body = json.dumps(
            {"event": "payment.captured", "payload": {"payment": {"entity": entity}}}
        ).encode()
        signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
        response = self.customer.post(
            "/api/webhooks/razorpay",
            data=body,
            headers={
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": "evt_test_qr_capture",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        connection = campusgo.connect_db()
        saved = connection.execute(
            "SELECT status FROM orders WHERE id = ?", (order["id"],)
        ).fetchone()
        connection.close()
        self.assertEqual(saved["status"], "PLACED")

    def test_printing_rejects_missing_page_count_and_client_price_is_ignored(self):
        xerox = self.product("Xerox copies (B&W)")
        invalid = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": [{"product_id": xerox["id"], "page_count": 0, "copies": 1}],
                "destination": "Library",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        order = self.create_order(
            [{"product_id": xerox["id"], "page_count": 10, "copies": 1, "price_paise": 1}]
        )
        self.assertEqual(order["subtotal_paise"], 3000)

    def test_configured_catalog_price_is_used_and_not_reset_on_restart(self):
        pen = self.product("Pen")
        with campusgo.app.app_context():
            campusgo.get_db().execute(
                "UPDATE products SET price_paise = 1250 WHERE id = ?", (pen["id"],)
            )
        campusgo.init_db()
        configured_pen = self.product("Pen")
        self.assertEqual(configured_pen["price_paise"], 1250)
        order = self.create_order([{"product_id": configured_pen["id"], "quantity": 1}])
        self.assertEqual(order["subtotal_paise"], 1250)

    def test_pdf_upload_creates_private_document_with_detected_page_count(self):
        response = self.upload_pdf()
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(document["page_count"], 5)
        stored_files = list(campusgo.PRIVATE_DOCUMENTS.glob("*.pdf"))
        self.assertEqual(len(stored_files), 1)
        self.assertNotEqual(stored_files[0].name, document["filename"])
        self.assertEqual(stored_files[0].read_bytes()[:5], b"%PDF-")

    def test_upload_rejects_non_pdf_and_invalid_pdf_content(self):
        bad_extension = self.upload_pdf("document.txt", b"not a pdf")
        self.assertEqual(bad_extension.status_code, 400)
        invalid_pdf = self.upload_pdf("document.pdf", b"%PDF-not really a pdf")
        self.assertEqual(invalid_pdf.status_code, 400)

    def test_owner_can_remove_an_unattached_private_upload(self):
        uploaded = self.upload_pdf().get_json()["document"]
        private_file = next(campusgo.PRIVATE_DOCUMENTS.glob("*.pdf"))
        response = self.customer.delete(
            f"/api/documents/{uploaded['id']}",
            headers={"X-CSRF-Token": self.customer_csrf},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(private_file.exists())
        with campusgo.app.app_context():
            saved = campusgo.get_db().execute(
                "SELECT 1 FROM order_documents WHERE id = ?", (uploaded["id"],)
            ).fetchone()
        self.assertIsNone(saved)

    def test_upload_rejects_files_over_twenty_megabytes(self):
        oversized = self.upload_pdf("large.pdf", b"%PDF-" + b"x" * (20 * 1024 * 1024))
        self.assertEqual(oversized.status_code, 413)
        self.assertIn("20 MB", oversized.get_json()["error"])

    def test_order_requires_owned_pdf_and_records_page_range_and_document(self):
        xerox = self.product("Xerox copies (B&W)")
        missing_document = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": [{"product_id": xerox["id"], "copies": 2, "page_range": "1-3,5"}],
                "destination": "Library",
            },
        )
        self.assertEqual(missing_document.status_code, 400)

        uploaded = self.upload_pdf().get_json()["document"]
        response = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": [{
                    "product_id": xerox["id"],
                    "copies": 2,
                    "page_range": "1-3,5",
                    "duplex": True,
                }],
                "document_id": uploaded["id"],
                "destination": "Library",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        order = response.get_json()["order"]
        self.assertEqual(order["document"]["page_count"], 5)
        self.assertEqual(order["items"][0]["page_count"], 4)
        self.assertEqual(order["items"][0]["billable_pages"], 8)
        self.assertEqual(order["items"][0]["page_range"], "1-3,5")
        self.assertEqual(order["items"][0]["duplex"], 1)
        self.assertEqual(
            (order["subtotal_paise"], order["delivery_fee_paise"],
             order["platform_fee_paise"], order["total_paise"]),
            (2400, 500, 400, 3300),
        )
        with campusgo.app.app_context():
            saved = campusgo.get_db().execute(
                "SELECT order_id FROM order_documents WHERE id = ?", (uploaded["id"],)
            ).fetchone()
        self.assertEqual(saved["order_id"], order["id"])

    def test_document_download_is_private_to_owning_customer(self):
        xerox = self.product("Xerox copies (B&W)")
        uploaded = self.upload_pdf().get_json()["document"]
        response = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": [{"product_id": xerox["id"], "copies": 1, "page_range": "all"}],
                "document_id": uploaded["id"],
                "destination": "Library",
            },
        )
        order = response.get_json()["order"]
        download = self.customer.get(f"/api/orders/{order['id']}/document")
        self.assertEqual(download.status_code, 200)
        self.assertIn(b"%PDF-", download.data[:1024])
        self.assertEqual(download.headers["Cache-Control"], "private, no-store")
        download.close()

        another_student = campusgo.app.test_client()
        self.register(another_student, "Another")
        denied = another_student.get(f"/api/orders/{order['id']}/document")
        self.assertEqual(denied.status_code, 404)
        unauthorized_upload = campusgo.app.test_client().post("/api/documents")
        self.assertEqual(unauthorized_upload.status_code, 401)

    def test_invalid_page_range_does_not_consume_uploaded_document(self):
        xerox = self.product("Xerox copies (B&W)")
        uploaded = self.upload_pdf().get_json()["document"]
        response = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": [{"product_id": xerox["id"], "copies": 1, "page_range": "1-6"}],
                "document_id": uploaded["id"],
                "destination": "Library",
            },
        )
        self.assertEqual(response.status_code, 400)
        with campusgo.app.app_context():
            saved = campusgo.get_db().execute(
                "SELECT order_id FROM order_documents WHERE id = ?", (uploaded["id"],)
            ).fetchone()
        self.assertIsNone(saved["order_id"])

    def test_premium_order_is_4800_paise_and_only_verified_capture_releases_it(self):
        premium = self.product("Bluebook — Premium")
        order = self.create_order([{"product_id": premium["id"], "quantity": 1}])
        self.assertEqual(
            (order["subtotal_paise"], order["delivery_fee_paise"],
             order["platform_fee_paise"], order["total_paise"]),
            (3000, 1500, 300, 4800),
        )
        self.assertEqual(order["status"], "PAYMENT_PENDING")
        self.assertEqual(order["payment_status"], "pending")
        before_payment_runner = campusgo.app.test_client()
        runner_csrf = self.register(before_payment_runner, "Early Runner")
        self.post(before_payment_runner, runner_csrf, "/api/availability", {"available": True})
        self.assertEqual(before_payment_runner.get("/api/orders").get_json()["available_orders"], [])

        paid = self.capture_order(order)
        self.assertEqual(paid.status_code, 200, paid.get_json())
        paid_order = paid.get_json()["order"]
        self.assertEqual(paid_order["status"], "PLACED")
        self.assertEqual(paid_order["payment_status"], "captured")
        self.assertEqual(
            [(item["recipient_type"], item["amount_paise"]) for item in paid_order["allocations"]],
            [("shop", 3000), ("runner", 1500), ("platform", 300)],
        )
        with campusgo.app.app_context():
            saved_payment = campusgo.get_db().execute(
                """SELECT amount_paise, currency, status, verification_status,
                          gateway_fee_paise, gateway_tax_paise
                   FROM payment_records WHERE order_id = ?""",
                (order["id"],),
            ).fetchone()
        self.assertEqual(saved_payment["amount_paise"], 4800)
        self.assertEqual(saved_payment["currency"], "INR")
        self.assertEqual(saved_payment["status"], "captured")
        self.assertEqual(saved_payment["verification_status"], "signature_and_api_verified")
        self.assertEqual((saved_payment["gateway_fee_paise"], saved_payment["gateway_tax_paise"]), (100, 18))
        self.assertFalse(campusgo.app.test_client().get("/api/pricing").get_json()["route_enabled"])
        self.assertIn(
            "route_unavailable",
            {item["settlement_status"] for item in paid_order["allocations"]},
        )
        available = before_payment_runner.get("/api/orders").get_json()["available_orders"]
        self.assertEqual([item["id"] for item in available], [order["id"]])

    def test_online_only_rejects_cash_payment_and_missing_gateway_configuration(self):
        product = self.product("Pen")
        rejected = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {
                "items": [{"product_id": product["id"], "quantity": 1}],
                "destination": "Library",
                "payment_method": "cod",
            },
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["error"], "Only online payment is supported.")
        os.environ.pop("RAZORPAY_KEY_ID")
        not_configured = self.post(
            self.customer,
            self.customer_csrf,
            "/api/orders",
            {"items": [{"product_id": product["id"], "quantity": 1}], "destination": "Library"},
        )
        self.assertEqual(not_configured.status_code, 503)
        self.assertIn("test-mode keys", not_configured.get_json()["error"])
        self.assertFalse(self.customer.get("/api/pricing").get_json()["payment_configured"])

    def test_invalid_signature_authorized_or_failed_payment_never_releases_order(self):
        product = self.product("Pen")
        order = self.create_order([{"product_id": product["id"], "quantity": 1}])
        with campusgo.app.app_context():
            payment = campusgo.get_db().execute(
                "SELECT gateway_order_id FROM payment_records WHERE order_id = ?",
                (order["id"],),
            ).fetchone()
        bad_signature = self.post(
            self.customer,
            self.customer_csrf,
            f"/api/orders/{order['id']}/verify-payment",
            {
                "razorpay_order_id": payment["gateway_order_id"],
                "razorpay_payment_id": "pay_invalid",
                "razorpay_signature": "invalid",
            },
        )
        self.assertEqual(bad_signature.status_code, 400)
        self.assertEqual(self.customer.get("/api/orders").get_json()["available_orders"], [])

        pending = self.capture_order(order, payment_status="authorized")
        self.assertEqual(pending.status_code, 202)
        self.assertEqual(pending.get_json()["payment_status"], "authorized")
        self.assertEqual(self.customer.get("/api/orders").get_json()["available_orders"], [])

        failed_payment_id = f"pay_test_{order['id']}"
        self.gateway_payments[failed_payment_id]["status"] = "failed"
        failed = self.post(
            self.customer,
            self.customer_csrf,
            f"/api/orders/{order['id']}/payment-failed",
            {"razorpay_payment_id": failed_payment_id},
        )
        self.assertEqual(failed.status_code, 200, failed.get_json())
        self.assertEqual(failed.get_json()["payment_status"], "failed")
        self.assertEqual(self.customer.get("/api/orders").get_json()["available_orders"], [])
        with campusgo.app.app_context():
            saved = campusgo.get_db().execute(
                """SELECT payments.status AS payment_status, orders.payment_status AS order_payment_status,
                          orders.status AS order_status
                   FROM payment_records AS payments JOIN orders ON orders.id = payments.order_id
                   WHERE orders.id = ?""",
                (order["id"],),
            ).fetchone()
        self.assertEqual(saved["payment_status"], "failed")
        self.assertEqual(saved["order_payment_status"], "failed")
        self.assertEqual(saved["order_status"], "PAYMENT_PENDING")

    def test_checkout_cancellation_and_duplicate_submission_reuse_gateway_order(self):
        product = self.product("Pen")
        payload = {"items": [{"product_id": product["id"], "quantity": 1}], "destination": "Library"}
        headers = {
            "X-CSRF-Token": self.customer_csrf,
            "X-Idempotency-Key": "same-checkout-attempt-key",
        }
        first = self.customer.post("/api/orders", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        order = first.get_json()["order"]
        gateway_order_count = len(self.gateway_orders)
        duplicate = self.customer.post("/api/orders", json=payload, headers=headers)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.get_json()["order"]["id"], order["id"])
        self.assertEqual(
            duplicate.get_json()["payment_checkout"]["gateway_order_id"],
            first.get_json()["payment_checkout"]["gateway_order_id"],
        )
        self.assertIsNone(duplicate.get_json()["payment_error"])
        self.assertEqual(len(self.gateway_orders), gateway_order_count)
        changed_payload = {
            "items": [{"product_id": product["id"], "quantity": 2}],
            "destination": "Library",
        }
        mismatched_duplicate = self.customer.post(
            "/api/orders", json=changed_payload, headers=headers
        )
        self.assertEqual(mismatched_duplicate.status_code, 409)
        self.assertEqual(len(self.gateway_orders), gateway_order_count)
        for _ in range(3):
            cancelled = self.post(
                self.customer,
                self.customer_csrf,
                f"/api/orders/{order['id']}/payment-cancelled",
            )
            self.assertEqual(cancelled.status_code, 200)
            retry = self.post(
                self.customer,
                self.customer_csrf,
                f"/api/orders/{order['id']}/payment",
            )
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(
                retry.get_json()["payment_checkout"]["gateway_order_id"],
                first.get_json()["payment_checkout"]["gateway_order_id"],
            )
        self.assertEqual(len(self.gateway_orders), gateway_order_count)
        saved = self.customer.get("/api/orders").get_json()["customer_orders"][0]
        self.assertEqual(saved["status"], "PAYMENT_PENDING")
        self.assertEqual(saved["payment_status"], "pending")
        self.assertEqual(self.customer.get("/api/orders").get_json()["available_orders"], [])

    def test_verified_webhook_is_idempotent_and_invalid_signature_is_rejected(self):
        product = self.product("Pen")
        order = self.create_order([{"product_id": product["id"], "quantity": 1}])
        self.capture_order(order)
        payment_id = f"pay_test_{order['id']}"
        entity = self.gateway_payments[payment_id]
        body = json.dumps(
            {"event": "payment.captured", "payload": {"payment": {"entity": entity}}},
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
        headers = {
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": "evt_test_capture",
        }
        accepted = self.customer.post("/api/webhooks/razorpay", data=body, headers=headers)
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        duplicate = self.customer.post("/api/webhooks/razorpay", data=body, headers=headers)
        self.assertTrue(duplicate.get_json()["duplicate"])
        replay_with_changed_event_id = self.customer.post(
            "/api/webhooks/razorpay",
            data=body,
            headers={
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": "evt_test_capture_replayed",
            },
        )
        self.assertTrue(replay_with_changed_event_id.get_json()["duplicate"])
        with campusgo.app.app_context():
            count = campusgo.get_db().execute(
                "SELECT COUNT(*) AS count FROM payment_webhook_events WHERE event_id = ?",
                ("evt_test_capture",),
            ).fetchone()["count"]
        self.assertEqual(count, 1)
        invalid = self.customer.post(
            "/api/webhooks/razorpay",
            data=body,
            headers={"X-Razorpay-Signature": "bad", "X-Razorpay-Event-Id": "evt_invalid"},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_route_allocations_only_record_gateway_created_transfers(self):
        os.environ["RAZORPAY_ROUTE_ENABLED"] = "true"
        os.environ["RAZORPAY_SHOP_LINKED_ACCOUNT_ID"] = "acc_shopapproved"
        premium = self.product("Bluebook — Premium")
        order = self.create_order([{"product_id": premium["id"], "quantity": 1}])
        self.assertEqual(self.capture_order(order).status_code, 200)
        runner = campusgo.app.test_client()
        runner_csrf = self.register(runner, "Route Runner")
        runner_user = runner.get("/api/me").get_json()["user"]
        unauthorized_mapping = campusgo.app.test_client().post(
            "/api/admin/route-accounts",
            json={"runner_user_id": runner_user["id"], "linked_account_id": "acc_runnerapproved"},
        )
        self.assertEqual(unauthorized_mapping.status_code, 401)
        mapping = campusgo.app.test_client().post(
            "/api/admin/route-accounts",
            json={"runner_user_id": runner_user["id"], "linked_account_id": "acc_runnerapproved"},
            headers={"X-Admin-Token": "test-admin-token"},
        )
        self.assertEqual(mapping.status_code, 200, mapping.get_json())
        self.post(runner, runner_csrf, "/api/availability", {"available": True})
        accepted = self.post(runner, runner_csrf, f"/api/orders/{order['id']}/accept")
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        with campusgo.app.app_context():
            transfer_rows = campusgo.get_db().execute(
                """SELECT recipient_type, linked_account_id, amount_paise, status,
                          gateway_transfer_id FROM payment_transfers WHERE order_id = ?
                   ORDER BY recipient_type""",
                (order["id"],),
            ).fetchall()
        self.assertEqual(
            [(row["recipient_type"], row["amount_paise"], row["status"]) for row in transfer_rows],
            [("runner", 1500, "created"), ("shop", 3000, "created")],
        )
        self.assertTrue(all(row["status"] != "processed" for row in transfer_rows))
        self.assertEqual(
            {row["linked_account_id"] for row in transfer_rows},
            {"acc_shopapproved", "acc_runnerapproved"},
        )
        transfer_id = transfer_rows[0]["gateway_transfer_id"]
        event = {
            "event": "transfer.processed",
            "payload": {"transfer": {"entity": {"id": transfer_id, "status": "processed"}}},
        }
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
        processed = self.customer.post(
            "/api/webhooks/razorpay",
            data=body,
            headers={
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": "evt_test_transfer",
            },
        )
        self.assertEqual(processed.status_code, 200)
        reconciliation = campusgo.app.test_client().get(
            "/api/admin/reconciliation", headers={"X-Admin-Token": "test-admin-token"}
        )
        self.assertEqual(reconciliation.status_code, 200, reconciliation.get_json())
        reconciled = next(
            item for item in reconciliation.get_json()["payments"]
            if item["order_id"] == order["id"]
        )
        self.assertEqual(reconciled["amount_paise"], 4800)
        self.assertEqual(len(reconciled["transfers"]), 2)

    def test_failed_webhook_records_failure_and_refund_events_are_reconciled(self):
        order = self.create_order([{"product_id": self.product("Pen")["id"], "quantity": 1}])
        self.capture_order(order)
        payment_id = f"pay_test_{order['id']}"
        gateway_order_id = self.gateway_payments[payment_id]["order_id"]

        def send_webhook(event_id, event_type, payload):
            body = json.dumps(
                {"event": event_type, "payload": payload}, separators=(",", ":")
            ).encode("utf-8")
            signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
            return self.customer.post(
                "/api/webhooks/razorpay",
                data=body,
                headers={
                    "X-Razorpay-Signature": signature,
                    "X-Razorpay-Event-Id": event_id,
                },
            )

        # A late failed attempt must not downgrade an already captured payment.
        failed_attempt = {
            "payment": {
                "entity": {
                    "id": "pay_test_late_failed_attempt",
                    "order_id": gateway_order_id,
                    "amount": order["total_paise"],
                    "currency": "INR",
                    "status": "failed",
                }
            }
        }
        self.assertEqual(send_webhook("evt_late_failure", "payment.failed", failed_attempt).status_code, 200)
        with campusgo.app.app_context():
            saved = campusgo.get_db().execute(
                "SELECT status FROM payment_records WHERE order_id = ?", (order["id"],)
            ).fetchone()
        self.assertEqual(saved["status"], "captured")

        refund = {
            "refund": {
                "entity": {
                    "id": "rfnd_test_partial",
                    "payment_id": payment_id,
                    "amount": 100,
                    "currency": "INR",
                    "status": "created",
                }
            }
        }
        created = send_webhook("evt_refund_created", "refund.created", refund)
        self.assertEqual(created.status_code, 200, created.get_json())
        self.assertEqual(
            self.customer.get("/api/orders").get_json()["customer_orders"][0]["payment_status"],
            "captured",
        )
        refund["refund"]["entity"]["status"] = "processed"
        processed = send_webhook("evt_refund_processed", "refund.processed", refund)
        self.assertEqual(processed.status_code, 200, processed.get_json())
        duplicate_processed = send_webhook("evt_refund_processed_duplicate", "refund.processed", refund)
        self.assertTrue(duplicate_processed.get_json()["duplicate"])
        with campusgo.app.app_context():
            saved = campusgo.get_db().execute(
                """SELECT payment_records.status, payment_records.refunded_paise,
                          orders.payment_status
                   FROM payment_records JOIN orders ON orders.id = payment_records.order_id
                   WHERE orders.id = ?""",
                (order["id"],),
            ).fetchone()
        self.assertEqual(saved["status"], "partially_refunded")
        self.assertEqual(saved["refunded_paise"], 100)
        self.assertEqual(saved["payment_status"], "partially_refunded")

    def test_acceptance_is_single_runner_after_verified_payment(self):
        product = self.product("Bluebook — Basic")
        order = self.create_order([{"product_id": product["id"], "quantity": 1}])
        order_id = order["id"]
        self.assertEqual(self.capture_order(order).status_code, 200)
        runner = campusgo.app.test_client()
        runner_csrf = self.register(runner, "Runner")
        self.post(runner, runner_csrf, "/api/availability", {"available": True})
        open_orders = runner.get("/api/orders").get_json()["available_orders"]
        self.assertEqual([item["id"] for item in open_orders], [order_id])
        accepted = self.post(runner, runner_csrf, f"/api/orders/{order_id}/accept")
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        self.assertEqual(accepted.get_json()["order"]["delivery_user_id"], 2)

        second_runner = campusgo.app.test_client()
        second_csrf = self.register(second_runner, "Second Runner")
        self.post(second_runner, second_csrf, "/api/availability", {"available": True})
        duplicate = self.post(second_runner, second_csrf, f"/api/orders/{order_id}/accept")
        self.assertEqual(duplicate.status_code, 409)

        for status in ("PICKUP_IN_PROGRESS", "ITEMS_COLLECTED", "OUT_FOR_DELIVERY", "DELIVERED"):
            result = self.post(
                runner,
                runner_csrf,
                f"/api/orders/{order_id}/status",
                {"status": status},
            )
            self.assertEqual(result.status_code, 200, result.get_json())

        confirmed = self.post(
            self.customer,
            self.customer_csrf,
            f"/api/orders/{order_id}/confirm",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())
        final_order = confirmed.get_json()["order"]
        self.assertEqual(final_order["status"], "COMPLETED")
        self.assertEqual(final_order["payment_status"], "captured")
        with campusgo.app.app_context():
            allocations = campusgo.get_db().execute(
                "SELECT amount_paise FROM payment_allocations WHERE order_id = ?",
                (order_id,),
            ).fetchall()
        self.assertEqual(
            sum(allocation["amount_paise"] for allocation in allocations),
            final_order["total_paise"],
        )

    def test_authentication_and_csrf_are_required_for_protected_actions(self):
        unauthenticated = campusgo.app.test_client()
        response = unauthenticated.post("/api/availability", json={"available": True})
        self.assertEqual(response.status_code, 401)
        missing_csrf = self.customer.post("/api/availability", json={"available": True})
        self.assertEqual(missing_csrf.status_code, 403)


if __name__ == "__main__":
    unittest.main()
