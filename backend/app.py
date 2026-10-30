import os
import re
import secrets
import sqlite3
import hashlib
import hmac
import json
import base64
from io import BytesIO
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_file, send_from_directory, session
from flask_cors import CORS
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")
DATABASE = ROOT / "instance" / "campusgo.sqlite3"
FRONTEND_DIST = ROOT.parent / "frontend" / "dist"
PRIVATE_DOCUMENTS = ROOT / "instance" / "private_documents"
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
XEROX_INITIAL_SELLING_PRICE_PAISE = 300
XEROX_BASE_COST_PAISE = 50
LOCATIONS = [
    "BTech Block",
    "Law Block",
    "Library",
]
PRODUCTS = [
    ("Bluebook — Basic", "Academic essentials", "Basic exam-ready answer book", 1000, "bluebook", 1200, 300, 0),
    ("Bluebook — Premium", "Academic essentials", "Premium exam-ready answer book", 3000, "bluebook", 1500, 300, 0),
    ("Xerox copies (B&W)", "Print & Xerox", "Black-and-white photocopy, per page", XEROX_INITIAL_SELLING_PRICE_PAISE, "page", 500, 0, 50, XEROX_BASE_COST_PAISE),
    ("Printout (B&W)", "Print & Xerox", "Black-and-white print, per page", 300, "page", 500, 0, 50),
    ("Printout (Colour)", "Print & Xerox", "Colour print, per page", 1000, "page", 500, 0, 50),
    ("Record sheets", "Stationery", "Pack of practical record sheets", 2500, "stationery", 500, 200, 0),
    ("Pen", "Stationery", "Blue or black ball pen", 1000, "stationery", 500, 200, 0),
    ("A4 sheets", "Stationery", "Pack of A4 paper", 5000, "stationery", 500, 200, 0),
]
STATUS_FLOW = {
    "ACCEPTED": "PICKUP_IN_PROGRESS",
    "PICKUP_IN_PROGRESS": "ITEMS_COLLECTED",
    "ITEMS_COLLECTED": "OUT_FOR_DELIVERY",
    "OUT_FOR_DELIVERY": "DELIVERED",
}
RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


class RazorpayError(Exception):
    pass


def razorpay_settings():
    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    mode = os.environ.get("RAZORPAY_MODE", "test").strip().lower()
    if not key_id or not key_secret:
        return None
    expected_prefix = "rzp_test_" if mode == "test" else "rzp_live_"
    if mode not in ("test", "live") or not key_id.startswith(expected_prefix):
        raise RazorpayError("Razorpay key ID does not match the configured payment mode.")
    return {"key_id": key_id, "key_secret": key_secret, "mode": mode}


def razorpay_request(method, path, payload=None):
    settings = razorpay_settings()
    if settings is None:
        raise RazorpayError("Online payments are not configured. Add Razorpay test-mode keys to the backend .env file.")
    token = base64.b64encode(
        f"{settings['key_id']}:{settings['key_secret']}".encode("utf-8")
    ).decode("ascii")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    gateway_request = Request(
        f"{RAZORPAY_API_BASE}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(gateway_request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        error.close()
        raise RazorpayError(f"Razorpay rejected the payment request (HTTP {error.code}).") from None
    except (URLError, TimeoutError, OSError):
        raise RazorpayError("Razorpay could not be reached. The payment remains pending; retry shortly.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RazorpayError("Razorpay returned an unreadable response. The payment remains pending.") from None
    if not isinstance(result, dict):
        raise RazorpayError("Razorpay returned an invalid response.")
    return result


def verify_webhook_signature(raw_body, signature, webhook_secret):
    if not signature or not webhook_secret:
        return False
    expected = hmac.new(
        webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_page_range(page_range, total_pages):
    if page_range == "all":
        return total_pages
    if not isinstance(page_range, str) or not page_range or len(page_range) > 500:
        raise ValueError("Enter a valid page range, such as 1-3,5.")
    pages = set()
    for segment in page_range.split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", segment.strip())
        if not match:
            raise ValueError("Enter a valid page range, such as 1-3,5.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > total_pages:
            raise ValueError(f"Page ranges must be between 1 and {total_pages}.")
        selected = set(range(start, end + 1))
        if pages.intersection(selected):
            raise ValueError("Page ranges cannot include a page more than once.")
        pages.update(selected)
    if not pages:
        raise ValueError("Select at least one page to print.")
    return len(pages)

app = Flask(__name__)
CORS(
    app,
    origins=["https://campusgo-frontend-xpwd.onrender.com"],
    supports_credentials=True,
    allow_headers=["Content-Type", "X-CSRF-Token"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "local-dev-only-change-before-deploy"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
    JSON_SORT_KEYS=False,
    MAX_CONTENT_LENGTH=MAX_DOCUMENT_BYTES + 64 * 1024,
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db():
    connection = connect_db()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            available INTEGER NOT NULL DEFAULT 0 CHECK (available IN (0, 1)),
            route_account_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
            pricing_type TEXT NOT NULL DEFAULT 'stationery',
            delivery_fee_paise INTEGER NOT NULL DEFAULT 500,
            platform_fee_paise INTEGER NOT NULL DEFAULT 200,
            platform_fee_per_page_paise INTEGER NOT NULL DEFAULT 0,
            unit_cost_paise INTEGER NOT NULL DEFAULT 0 CHECK (unit_cost_paise >= 0),
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES users(id),
            delivery_user_id INTEGER REFERENCES users(id),
            destination TEXT NOT NULL,
            instructions TEXT NOT NULL DEFAULT '',
            item_total_paise INTEGER NOT NULL,
            subtotal_paise INTEGER NOT NULL DEFAULT 0,
            delivery_fee_paise INTEGER NOT NULL,
            platform_fee_paise INTEGER NOT NULL,
            total_paise INTEGER NOT NULL,
            status TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'online' CHECK (payment_method = 'online'),
            payment_status TEXT NOT NULL,
            idempotency_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id),
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 20),
            unit_price_paise INTEGER NOT NULL,
            unit_cost_paise INTEGER NOT NULL DEFAULT 0 CHECK (unit_cost_paise >= 0),
            page_count INTEGER NOT NULL DEFAULT 0,
            copies INTEGER NOT NULL DEFAULT 0,
            billable_pages INTEGER NOT NULL DEFAULT 0,
            page_range TEXT NOT NULL DEFAULT 'all',
            duplex INTEGER NOT NULL DEFAULT 0 CHECK (duplex IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS order_status_history (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            actor_id INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_allocations (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            recipient_type TEXT NOT NULL CHECK (recipient_type IN ('shop', 'runner', 'platform')),
            recipient_user_id INTEGER REFERENCES users(id),
            amount_paise INTEGER NOT NULL CHECK (amount_paise >= 0),
            settlement_status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_records (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
            gateway_order_id TEXT UNIQUE,
            gateway_qr_code_id TEXT UNIQUE,
            qr_image_url TEXT,
            qr_close_by INTEGER,
            gateway_payment_id TEXT UNIQUE,
            amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
            currency TEXT NOT NULL DEFAULT 'INR',
            status TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            gateway_fee_paise INTEGER NOT NULL DEFAULT 0 CHECK (gateway_fee_paise >= 0),
            gateway_tax_paise INTEGER NOT NULL DEFAULT 0 CHECK (gateway_tax_paise >= 0),
            refunded_paise INTEGER NOT NULL DEFAULT 0 CHECK (refunded_paise >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_transfers (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            recipient_type TEXT NOT NULL CHECK (recipient_type IN ('shop', 'runner')),
            recipient_user_id INTEGER REFERENCES users(id),
            linked_account_id TEXT NOT NULL,
            amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
            gateway_transfer_id TEXT UNIQUE,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (order_id, recipient_type)
        );
        CREATE TABLE IF NOT EXISTS payment_refunds (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            gateway_refund_id TEXT NOT NULL UNIQUE,
            amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL UNIQUE,
            processing_status TEXT NOT NULL,
            received_at TEXT NOT NULL,
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS system_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_documents (
            id INTEGER PRIMARY KEY,
            document_key TEXT NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL REFERENCES users(id),
            order_id INTEGER UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
            original_filename TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 20971520),
            page_count INTEGER NOT NULL CHECK (page_count BETWEEN 1 AND 1000),
            created_at TEXT NOT NULL,
            attached_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_orders_open ON orders(status, payment_status);
        CREATE INDEX IF NOT EXISTS idx_allocations_order ON payment_allocations(order_id);
        CREATE INDEX IF NOT EXISTS idx_transfers_order ON payment_transfers(order_id);
        CREATE INDEX IF NOT EXISTS idx_documents_owner ON order_documents(customer_id, order_id);
        """
    )
    migrations = {
        "products": {
            "pricing_type": "TEXT NOT NULL DEFAULT 'stationery'",
            "delivery_fee_paise": "INTEGER NOT NULL DEFAULT 500",
            "platform_fee_paise": "INTEGER NOT NULL DEFAULT 200",
            "platform_fee_per_page_paise": "INTEGER NOT NULL DEFAULT 0",
            "unit_cost_paise": "INTEGER NOT NULL DEFAULT 0",
        },
        "orders": {
            "subtotal_paise": "INTEGER NOT NULL DEFAULT 0",
            "idempotency_key": "TEXT",
            "request_fingerprint": "TEXT",
        },
        "users": {"route_account_id": "TEXT"},
        "payment_records": {
            "refunded_paise": "INTEGER NOT NULL DEFAULT 0",
            "gateway_qr_code_id": "TEXT",
            "qr_image_url": "TEXT",
            "qr_close_by": "INTEGER",
        },
        "order_items": {
            "unit_cost_paise": "INTEGER NOT NULL DEFAULT 0",
            "page_count": "INTEGER NOT NULL DEFAULT 0",
            "copies": "INTEGER NOT NULL DEFAULT 0",
            "billable_pages": "INTEGER NOT NULL DEFAULT 0",
            "page_range": "TEXT NOT NULL DEFAULT 'all'",
            "duplex": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in migrations.items():
        existing_columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in columns.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_customer_idempotency "
        "ON orders(customer_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_records_qr_code "
        "ON payment_records(gateway_qr_code_id) WHERE gateway_qr_code_id IS NOT NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhooks_payload_sha256 "
        "ON payment_webhook_events(payload_sha256)"
    )
    connection.execute(
        """UPDATE orders SET status = 'CANCELLED', payment_status = 'cancelled',
           updated_at = ? WHERE payment_method = 'cod'
           AND status NOT IN ('COMPLETED', 'CANCELLED')""",
        (now(),),
    )
    connection.execute(
        """UPDATE orders SET status = 'CANCELLED', payment_status = 'cancelled',
           updated_at = ? WHERE payment_status IN
             ('pending_demo', 'demo_paid', 'demo_paid_unsettled')""",
        (now(),),
    )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS orders_online_only_insert
        BEFORE INSERT ON orders
        WHEN NEW.payment_method != 'online'
        BEGIN
            SELECT RAISE(ABORT, 'Only online payment is supported.');
        END;
        CREATE TRIGGER IF NOT EXISTS orders_online_only_update
        BEFORE UPDATE OF payment_method ON orders
        WHEN NEW.payment_method != 'online'
        BEGIN
            SELECT RAISE(ABORT, 'Only online payment is supported.');
        END;
        """
    )
    catalog_seeded = connection.execute(
        "SELECT 1 FROM system_metadata WHERE key = 'catalog_pricing_seeded'"
    ).fetchone()
    if not catalog_seeded:
        connection.execute("UPDATE products SET active = 0 WHERE name = 'Bluebook'")
    for product in PRODUCTS:
        name, category, description, price, pricing_type, delivery, platform, per_page = product[:8]
        unit_cost = product[8] if len(product) > 8 else 0
        if catalog_seeded:
            existing = connection.execute(
                "SELECT 1 FROM products WHERE name = ?", (name,)
            ).fetchone()
        else:
            connection.execute(
                """UPDATE products SET category = ?, description = ?, price_paise = ?,
                   pricing_type = ?, delivery_fee_paise = ?, platform_fee_paise = ?,
                   platform_fee_per_page_paise = ?, unit_cost_paise = ?, active = 1 WHERE name = ?""",
                (category, description, price, pricing_type, delivery, platform, per_page, unit_cost, name),
            )
            existing = connection.execute(
                "SELECT 1 FROM products WHERE name = ?", (name,)
            ).fetchone()
        if not existing:
            connection.execute(
                """INSERT INTO products
                   (name, category, description, price_paise, pricing_type, delivery_fee_paise,
                    platform_fee_paise, platform_fee_per_page_paise, unit_cost_paise, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (name, category, description, price, pricing_type, delivery, platform, per_page, unit_cost),
            )
    file_product_retired = connection.execute(
        "SELECT 1 FROM system_metadata WHERE key = 'file_product_retired'"
    ).fetchone()
    if not file_product_retired:
        connection.execute("UPDATE products SET active = 0 WHERE name = 'File'")
        connection.execute(
            "INSERT INTO system_metadata (key, value) VALUES ('file_product_retired', 'true')"
        )
    xerox_pricing_migrated = connection.execute(
        "SELECT 1 FROM system_metadata WHERE key = 'xerox_pricing_v2'"
    ).fetchone()
    if not xerox_pricing_migrated:
        connection.execute(
            """UPDATE products SET price_paise = ?, unit_cost_paise = ?
               WHERE name = 'Xerox copies (B&W)'""",
            (XEROX_INITIAL_SELLING_PRICE_PAISE, XEROX_BASE_COST_PAISE),
        )
        connection.execute(
            """UPDATE order_items SET unit_cost_paise = ?
               WHERE product_id = (
                   SELECT id FROM products WHERE name = 'Xerox copies (B&W)'
               ) AND unit_cost_paise = 0""",
            (XEROX_BASE_COST_PAISE,),
        )
        connection.execute(
            "INSERT INTO system_metadata (key, value) VALUES ('xerox_pricing_v2', 'true')"
        )
    if not catalog_seeded:
        connection.execute(
            "INSERT INTO system_metadata (key, value) VALUES ('catalog_pricing_seeded', 'true')"
        )
    connection.close()


def calculate_order_totals(items):
    item_subtotal = 0
    delivery_fee = 0
    platform_fee = 0
    stationery_fee = 0
    priced_items = []

    for product, quantity, page_count, copies, page_range, duplex in items:
        pricing_type = product["pricing_type"]
        billable_pages = page_count * copies if pricing_type == "page" else 0
        billed_quantity = billable_pages if pricing_type == "page" else quantity
        line_subtotal = product["price_paise"] * billed_quantity
        item_subtotal += line_subtotal
        delivery_fee = max(delivery_fee, product["delivery_fee_paise"])

        if pricing_type == "page":
            platform_fee += product["platform_fee_per_page_paise"] * billable_pages
        elif pricing_type == "bluebook":
            platform_fee += product["platform_fee_paise"] * quantity
        else:
            stationery_fee = max(stationery_fee, product["platform_fee_paise"])

        priced_items.append(
            {
                "product": product,
                "quantity": quantity,
                "page_count": page_count,
                "copies": copies,
                "billable_pages": billable_pages,
                "page_range": page_range,
                "duplex": duplex,
                "line_subtotal_paise": line_subtotal,
                "unit_cost_paise": product["unit_cost_paise"],
            }
        )

    platform_fee += stationery_fee
    return {
        "item_subtotal_paise": item_subtotal,
        "delivery_fee_paise": delivery_fee,
        "platform_fee_paise": platform_fee,
        "total_paise": item_subtotal + delivery_fee + platform_fee,
        "items": priced_items,
    }


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute(
        "SELECT id, name, email, available FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def require_auth(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return jsonify(error="Please log in to continue."), 401
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            expected = session.get("csrf_token")
            supplied = request.headers.get("X-CSRF-Token")
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                return jsonify(error="Your session expired. Refresh and try again."), 403
        return handler(*args, **kwargs)

    return wrapped


def user_json(user):
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "available": bool(user["available"]),
    }


def auth_response(user, status=200):
    session.clear()
    session["user_id"] = user["id"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify(user=user_json(user), csrf_token=session["csrf_token"]), status


def add_history(connection, order_id, status, actor_id):
    connection.execute(
        "INSERT INTO order_status_history (order_id, status, actor_id, created_at) VALUES (?, ?, ?, ?)",
        (order_id, status, actor_id, now()),
    )


def order_json(connection, row, include_history=False, include_document=False, include_qr=False):
    items = connection.execute(
        """SELECT product_name, quantity, unit_price_paise, page_count, copies,
                  billable_pages, page_range, duplex,
                  page_count * copies AS billable_page_count
           FROM order_items WHERE order_id = ? ORDER BY id""",
        (row["id"],),
    ).fetchall()
    result = dict(row)
    result.pop("idempotency_key", None)
    result.pop("request_fingerprint", None)
    result["items"] = [dict(item) for item in items]
    if include_document:
        document = connection.execute(
            """SELECT original_filename, byte_size, page_count FROM order_documents
               WHERE order_id = ? AND customer_id = ?""",
            (row["id"], row["customer_id"]),
        ).fetchone()
        result["document"] = dict(document) if document else None
    if result["delivery_user_id"]:
        runner = connection.execute(
            "SELECT name FROM users WHERE id = ?", (result["delivery_user_id"],)
        ).fetchone()
        result["runner_name"] = runner["name"] if runner else None
    if include_history:
        history = connection.execute(
            """SELECT status, created_at FROM order_status_history
               WHERE order_id = ? ORDER BY id""",
            (row["id"],),
        ).fetchall()
        result["history"] = [dict(item) for item in history]
    payment = connection.execute(
        """SELECT status, verification_status, gateway_fee_paise, gateway_tax_paise
           FROM payment_records WHERE order_id = ?""",
        (row["id"],),
    ).fetchone()
    result["payment"] = dict(payment) if payment else None
    result["qr_payment"] = None
    if include_qr:
        qr_payment = connection.execute(
            """SELECT gateway_qr_code_id, qr_image_url, qr_close_by
               FROM payment_records WHERE order_id = ?""",
            (row["id"],),
        ).fetchone()
        if qr_payment and qr_payment["gateway_qr_code_id"] and qr_payment["qr_image_url"]:
            result["qr_payment"] = payment_qr_json(
                qr_payment["gateway_qr_code_id"],
                qr_payment["qr_image_url"],
                qr_payment["qr_close_by"],
            )
    result["allocations"] = [
        dict(allocation)
        for allocation in connection.execute(
            """SELECT recipient_type, amount_paise, settlement_status
               FROM payment_allocations WHERE order_id = ? ORDER BY id""",
            (row["id"],),
        ).fetchall()
    ]
    return result


def payment_checkout_json(order, gateway_order_id):
    settings = razorpay_settings()
    if settings is None:
        raise RazorpayError(
            "Online payments are not configured. Add Razorpay test-mode keys to the backend .env file."
        )
    return {
        "key_id": settings["key_id"],
        "gateway_order_id": gateway_order_id,
        "amount_paise": order["total_paise"],
        "currency": "INR",
        "mode": settings["mode"],
    }


def payment_qr_json(qr_code_id, image_url, close_by):
    return {
        "qr_code_id": qr_code_id,
        "image_url": image_url,
        "close_by": close_by,
    }


def start_qr_payment(connection, order):
    if os.environ.get("RAZORPAY_QR_ENABLED", "false").strip().lower() != "true":
        raise RazorpayError(
            "Razorpay QR Code payments are disabled. Enable QR Code payments for your merchant account and set RAZORPAY_QR_ENABLED=true."
        )
    settings = razorpay_settings()
    if settings is None:
        raise RazorpayError(
            "Online payments are not configured. Add Razorpay test-mode keys to the backend .env file."
        )
    connection.execute("BEGIN IMMEDIATE")
    existing = connection.execute(
        "SELECT * FROM payment_records WHERE order_id = ?", (order["id"],)
    ).fetchone()
    if existing and existing["status"] == "captured":
        connection.commit()
        raise RazorpayError("This order has already been paid.")
    if existing and existing["status"] == "authorized":
        connection.commit()
        raise RazorpayError(
            "Payment is authorized and awaiting capture confirmation. Please wait before retrying."
        )
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    if (
        existing
        and existing["gateway_qr_code_id"]
        and existing["qr_image_url"]
        and existing["qr_close_by"]
        and existing["qr_close_by"] > now_epoch
    ):
        connection.commit()
        return payment_qr_json(
            existing["gateway_qr_code_id"],
            existing["qr_image_url"],
            existing["qr_close_by"],
        )
    if existing and existing["status"] == "creating":
        connection.rollback()
        raise RazorpayError("Payment setup is already in progress. Please wait and retry.")
    stamp = now()
    if existing:
        connection.execute(
            """UPDATE payment_records SET status = 'creating', updated_at = ?
               WHERE order_id = ?""",
            (stamp, order["id"]),
        )
    else:
        connection.execute(
            """INSERT INTO payment_records
               (order_id, amount_paise, currency, status, verification_status,
                created_at, updated_at)
               VALUES (?, ?, 'INR', 'creating', 'pending', ?, ?)""",
            (order["id"], order["total_paise"], stamp, stamp),
        )
    connection.commit()
    close_by = now_epoch + 2 * 60 * 60
    try:
        qr_code = razorpay_request(
            "POST",
            "/payments/qr_codes",
            {
                "type": "upi_qr",
                "name": f"CampusGo order {order['id']}",
                "usage": "single_use",
                "fixed_amount": True,
                "payment_amount": order["total_paise"],
                "description": f"CampusGo order #{order['id']}",
                "close_by": close_by,
                "notes": {"campusgo_order_id": str(order["id"])},
            },
        )
        image_url = qr_code.get("image_url")
        parsed_image_url = urlparse(image_url) if isinstance(image_url, str) else None
        if (
            not isinstance(qr_code.get("id"), str)
            or not qr_code["id"].startswith("qr_")
            or qr_code.get("payment_amount") != order["total_paise"]
            or qr_code.get("fixed_amount") is not True
            or qr_code.get("usage") != "single_use"
            or qr_code.get("status") != "active"
            or parsed_image_url is None
            or parsed_image_url.scheme != "https"
            or parsed_image_url.hostname != "rzp.io"
            or not isinstance(qr_code.get("close_by"), int)
        ):
            raise RazorpayError(
                "Razorpay returned a QR code that did not match the requested order amount."
            )
    except RazorpayError:
        connection.execute(
            """UPDATE payment_records SET status = 'initialization_failed', updated_at = ?
               WHERE order_id = ? AND status = 'creating'""",
            (now(), order["id"]),
        )
        connection.commit()
        raise
    connection.execute(
        """UPDATE payment_records SET gateway_qr_code_id = ?, qr_image_url = ?,
           qr_close_by = ?, status = 'pending', verification_status = 'pending',
           updated_at = ? WHERE order_id = ?""",
        (qr_code["id"], image_url, qr_code["close_by"], now(), order["id"]),
    )
    connection.commit()
    return payment_qr_json(qr_code["id"], image_url, qr_code["close_by"])


def start_gateway_order(connection, order):
    connection.execute("BEGIN IMMEDIATE")
    existing = connection.execute(
        "SELECT * FROM payment_records WHERE order_id = ?", (order["id"],)
    ).fetchone()
    if existing and existing["gateway_order_id"]:
        if existing["status"] == "captured":
            connection.commit()
            raise RazorpayError("This order has already been paid.")
        if existing["status"] == "authorized":
            connection.commit()
            raise RazorpayError(
                "Payment is authorized and awaiting capture confirmation. Please wait before retrying."
            )
        if existing["status"] in ("cancelled", "failed"):
            stamp = now()
            connection.execute(
                """UPDATE payment_records SET status = 'pending',
                   verification_status = 'pending', updated_at = ? WHERE order_id = ?""",
                (stamp, order["id"]),
            )
            connection.execute(
                """UPDATE orders SET payment_status = 'pending', updated_at = ?
                   WHERE id = ? AND status = 'PAYMENT_PENDING'""",
                (stamp, order["id"]),
            )
        connection.commit()
        return payment_checkout_json(order, existing["gateway_order_id"])
    stamp = now()
    if existing and existing["status"] == "creating":
        connection.rollback()
        raise RazorpayError("Payment setup is already in progress. Please wait and retry.")
    if existing:
        connection.execute(
            """UPDATE payment_records SET status = 'creating', updated_at = ?
               WHERE order_id = ?""",
            (stamp, order["id"]),
        )
    else:
        connection.execute(
            """INSERT INTO payment_records
               (order_id, amount_paise, currency, status, verification_status,
                created_at, updated_at)
               VALUES (?, ?, 'INR', 'creating', 'pending', ?, ?)""",
            (order["id"], order["total_paise"], stamp, stamp),
        )
    connection.commit()
    try:
        gateway_order = razorpay_request(
            "POST",
            "/orders",
            {
                "amount": order["total_paise"],
                "currency": "INR",
                "receipt": f"campusgo_{order['id']}",
                "partial_payment": False,
                "notes": {"campusgo_order_id": str(order["id"])},
            },
        )
        if (
            not isinstance(gateway_order.get("id"), str)
            or gateway_order.get("amount") != order["total_paise"]
            or gateway_order.get("currency") != "INR"
        ):
            raise RazorpayError("Razorpay order amount did not match the verified CampusGo total.")
    except RazorpayError:
        connection.execute(
            """UPDATE payment_records SET status = 'initialization_failed', updated_at = ?
               WHERE order_id = ? AND status = 'creating'""",
            (now(), order["id"]),
        )
        connection.commit()
        raise
    connection.execute(
        """UPDATE payment_records SET gateway_order_id = ?, status = 'pending',
           verification_status = 'pending', updated_at = ? WHERE order_id = ?""",
        (gateway_order["id"], now(), order["id"]),
    )
    connection.commit()
    return payment_checkout_json(order, gateway_order["id"])


def mark_payment_captured(connection, payment_entity, verification_status):
    gateway_order_id = payment_entity.get("order_id")
    gateway_qr_code_id = payment_entity.get("qr_code_id")
    payment_id = payment_entity.get("id")
    if (not gateway_order_id and not gateway_qr_code_id) or not payment_id:
        return False
    payment_record = connection.execute(
        """SELECT payments.*, orders.id AS campus_order_id, orders.total_paise,
                  orders.status AS order_status
           FROM payment_records AS payments
           JOIN orders ON orders.id = payments.order_id
           WHERE (payments.gateway_order_id = ? AND ? IS NOT NULL)
              OR (payments.gateway_qr_code_id = ? AND ? IS NOT NULL)""",
        (gateway_order_id, gateway_order_id, gateway_qr_code_id, gateway_qr_code_id),
    ).fetchone()
    if (
        payment_record is None
        or (
            gateway_order_id
            and gateway_order_id != payment_record["gateway_order_id"]
        )
        or (
            gateway_qr_code_id
            and gateway_qr_code_id != payment_record["gateway_qr_code_id"]
        )
        or payment_entity.get("amount") != payment_record["amount_paise"]
        or payment_entity.get("currency") != "INR"
        or payment_entity.get("status") != "captured"
    ):
        return False
    if payment_record["status"] in ("refunded", "partially_refunded"):
        return True
    if payment_record["status"] == "captured":
        return payment_record["gateway_payment_id"] == payment_id
    stamp = now()
    fee = payment_entity.get("fee", 0)
    tax = payment_entity.get("tax", 0)
    if not isinstance(fee, int) or fee < 0:
        fee = 0
    if not isinstance(tax, int) or tax < 0:
        tax = 0
    connection.execute("BEGIN IMMEDIATE")
    current = connection.execute(
        "SELECT status FROM payment_records WHERE order_id = ?",
        (payment_record["campus_order_id"],),
    ).fetchone()
    if current is None:
        connection.rollback()
        return False
    connection.execute(
        """UPDATE payment_records SET gateway_payment_id = ?, status = 'captured',
           verification_status = ?, gateway_fee_paise = ?, gateway_tax_paise = ?,
           updated_at = ? WHERE order_id = ?""",
        (
            payment_id, verification_status, fee, tax, stamp,
            payment_record["campus_order_id"],
        ),
    )
    if payment_record["order_status"] == "PAYMENT_PENDING":
        connection.execute(
            """UPDATE orders SET status = 'PLACED', payment_status = 'captured',
               updated_at = ? WHERE id = ? AND status = 'PAYMENT_PENDING'""",
            (stamp, payment_record["campus_order_id"]),
        )
        add_history(
            connection,
            payment_record["campus_order_id"],
            "PLACED",
            None,
        )
    else:
        connection.execute(
            "UPDATE orders SET payment_status = 'captured', updated_at = ? WHERE id = ?",
            (stamp, payment_record["campus_order_id"]),
        )
    connection.commit()
    return True


def update_payment_failure(connection, gateway_order_id, payment_id, status):
    payment = connection.execute(
        """SELECT order_id FROM payment_records
           WHERE gateway_order_id = ? OR gateway_qr_code_id = ?""",
        (gateway_order_id, gateway_order_id),
    ).fetchone()
    if payment is None:
        return False
    stamp = now()
    connection.execute(
        """UPDATE payment_records SET gateway_payment_id = COALESCE(?, gateway_payment_id),
           status = ?, verification_status = 'webhook_verified', updated_at = ?
           WHERE order_id = ? AND status != 'captured'""",
        (payment_id, status, stamp, payment["order_id"]),
    )
    connection.execute(
        """UPDATE orders SET payment_status = ?, updated_at = ?
           WHERE id = ? AND status = 'PAYMENT_PENDING'""",
        (status, stamp, payment["order_id"]),
    )
    return True


def attempt_route_transfers(connection, order_id):
    payment = connection.execute(
        """SELECT payment_records.gateway_payment_id, orders.total_paise,
                  orders.subtotal_paise, orders.delivery_fee_paise,
                  orders.platform_fee_paise, orders.delivery_user_id,
                  payment_records.gateway_fee_paise, payment_records.gateway_tax_paise,
                  users.route_account_id AS runner_account_id
           FROM payment_records JOIN orders ON orders.id = payment_records.order_id
           LEFT JOIN users ON users.id = orders.delivery_user_id
           WHERE orders.id = ? AND payment_records.status = 'captured'""",
        (order_id,),
    ).fetchone()
    if payment is None:
        return
    shop_account = os.environ.get("RAZORPAY_SHOP_LINKED_ACCOUNT_ID", "").strip()
    route_enabled = os.environ.get("RAZORPAY_ROUTE_ENABLED", "false").lower() == "true"
    if not route_enabled:
        connection.execute(
            """UPDATE payment_allocations SET settlement_status = 'route_unavailable'
               WHERE order_id = ? AND recipient_type IN ('shop', 'runner')""",
            (order_id,),
        )
        connection.execute(
            """UPDATE payment_allocations SET settlement_status = 'retained_by_platform'
               WHERE order_id = ? AND recipient_type = 'platform'""",
            (order_id,),
        )
        connection.commit()
        return

    required_recipients = [
        ("shop", shop_account, payment["subtotal_paise"]),
        ("runner", payment["runner_account_id"], payment["delivery_fee_paise"]),
    ]
    if not payment["delivery_user_id"]:
        connection.execute(
            """UPDATE payment_allocations SET settlement_status = 'pending_assignment'
               WHERE order_id = ? AND recipient_type = 'runner'""",
            (order_id,),
        )
        required_recipients = required_recipients[:1]
    available_recipients = [
        (kind, account_id, amount)
        for kind, account_id, amount in required_recipients
        if account_id and amount > 0
    ]
    for kind, account_id, amount in required_recipients:
        if not account_id and amount > 0:
            connection.execute(
                """UPDATE payment_allocations SET settlement_status = 'linked_account_unavailable'
                   WHERE order_id = ? AND recipient_type = ?""",
                (order_id, kind),
            )
    connection.execute(
        """UPDATE payment_allocations SET settlement_status = 'retained_by_platform'
           WHERE order_id = ? AND recipient_type = 'platform'""",
        (order_id,),
    )
    if not available_recipients:
        connection.commit()
        return
    available_total = payment["total_paise"] - payment["gateway_fee_paise"] - payment["gateway_tax_paise"]
    if sum(amount for _kind, _account_id, amount in available_recipients) > available_total:
        connection.execute(
            """UPDATE payment_allocations SET settlement_status = 'amount_unavailable_after_gateway_fees'
               WHERE order_id = ? AND recipient_type IN ('shop', 'runner')""",
            (order_id,),
        )
        connection.commit()
        return

    existing = {
        row["recipient_type"]
        for row in connection.execute(
            "SELECT recipient_type FROM payment_transfers WHERE order_id = ?",
            (order_id,),
        ).fetchall()
    }
    available_recipients = [
        item for item in available_recipients if item[0] not in existing
    ]
    if not available_recipients:
        connection.commit()
        return
    stamp = now()
    connection.execute("BEGIN IMMEDIATE")
    for kind, account_id, amount in available_recipients:
        runner_id = payment["delivery_user_id"] if kind == "runner" else None
        connection.execute(
            """INSERT INTO payment_transfers
               (order_id, recipient_type, recipient_user_id, linked_account_id,
                amount_paise, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'creating', ?, ?)""",
            (order_id, kind, runner_id, account_id, amount, stamp, stamp),
        )
        connection.execute(
            """UPDATE payment_allocations SET settlement_status = 'transfer_pending'
               WHERE order_id = ? AND recipient_type = ?""",
            (order_id, kind),
        )
    connection.commit()
    transfers = [
        {
            "account": account_id,
            "amount": amount,
            "currency": "INR",
            "notes": {"campusgo_order_id": str(order_id), "recipient_type": kind},
        }
        for kind, account_id, amount in available_recipients
    ]
    try:
        result = razorpay_request(
            "POST",
            f"/payments/{payment['gateway_payment_id']}/transfers",
            {"transfers": transfers},
        )
        entities = result.get("items")
        if not isinstance(entities, list) or len(entities) != len(available_recipients):
            raise RazorpayError("Razorpay transfer response requires reconciliation.")
        for expected, entity in zip(available_recipients, entities):
            kind, account_id, amount = expected
            if (
                entity.get("account") != account_id
                or entity.get("amount") != amount
                or not entity.get("id")
            ):
                raise RazorpayError("Razorpay transfer response did not match the approved allocation.")
            transfer_status = entity.get("status", "created")
            connection.execute(
                """UPDATE payment_transfers SET gateway_transfer_id = ?, status = ?,
                   updated_at = ? WHERE order_id = ? AND recipient_type = ?""",
                (entity["id"], transfer_status, now(), order_id, kind),
            )
            connection.execute(
                """UPDATE payment_allocations SET settlement_status = ?
                   WHERE order_id = ? AND recipient_type = ?""",
                (transfer_status, order_id, kind),
            )
    except RazorpayError:
        connection.execute(
            """UPDATE payment_transfers SET status = 'pending_verification', updated_at = ?
               WHERE order_id = ? AND status = 'creating'""",
            (now(), order_id),
        )
        connection.execute(
            """UPDATE payment_allocations SET settlement_status = 'pending_verification'
               WHERE order_id = ? AND settlement_status = 'transfer_pending'""",
            (order_id,),
        )
    connection.commit()


@app.get("/api/me")
def me():
    user = current_user()
    if user is None:
        return jsonify(user=None, csrf_token=None)
    return jsonify(user=user_json(user), csrf_token=session.get("csrf_token"))


@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    if not name or len(name) > 80:
        return jsonify(error="Enter your name (up to 80 characters)."), 400
    if len(email) > 254 or "@" not in email:
        return jsonify(error="Enter a valid email address."), 400
    if not isinstance(password, str) or len(password) < 10:
        return jsonify(error="Use a password with at least 10 characters."), 400
    connection = get_db()
    try:
        cursor = connection.execute(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash(password), now()),
        )
    except sqlite3.IntegrityError:
        return jsonify(error="An account with that email already exists."), 409
    user = connection.execute(
        "SELECT id, name, email, available FROM users WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return auth_response(user, 201)


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None or not isinstance(password, str) or not check_password_hash(user["password_hash"], password):
        return jsonify(error="Email or password is incorrect."), 401
    return auth_response(user)


@app.post("/api/auth/logout")
@require_auth
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/catalog")
def catalog():
    products = get_db().execute(
        """SELECT id, name, category, description, price_paise, pricing_type,
                  delivery_fee_paise, platform_fee_paise, platform_fee_per_page_paise
           FROM products WHERE active = 1 ORDER BY id"""
    ).fetchall()
    return jsonify(products=[dict(product) for product in products], locations=LOCATIONS)


@app.route("/api/admin/pricing/xerox", methods=["GET", "PUT"])
def manage_xerox_pricing():
    admin_token = os.environ.get("CAMPUSGO_ADMIN_TOKEN", "").strip()
    supplied_token = request.headers.get("X-Admin-Token", "")
    if not admin_token:
        return jsonify(error="Xerox pricing management is not configured."), 503
    if not supplied_token or not hmac.compare_digest(admin_token, supplied_token):
        return jsonify(error="Not authorized."), 401

    connection = get_db()
    product = connection.execute(
        """SELECT id, price_paise, unit_cost_paise, platform_fee_per_page_paise,
                  delivery_fee_paise
           FROM products WHERE name = 'Xerox copies (B&W)' AND active = 1"""
    ).fetchone()
    if product is None:
        return jsonify(error="Xerox product is not configured."), 404

    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Provide the customer selling price in paise."), 400
        selling_price_paise = data.get("selling_price_paise")
        if (
            isinstance(selling_price_paise, bool)
            or not isinstance(selling_price_paise, int)
            or not 1 <= selling_price_paise <= 2_147_483_647
        ):
            return jsonify(
                error="Set the Xerox customer selling price as a positive whole number of paise."
            ), 400
        connection.execute(
            "UPDATE products SET price_paise = ? WHERE id = ?",
            (selling_price_paise, product["id"]),
        )
        product = connection.execute(
            """SELECT id, price_paise, unit_cost_paise, platform_fee_per_page_paise,
                      delivery_fee_paise
               FROM products WHERE id = ?""",
            (product["id"],),
        ).fetchone()

    return jsonify(
        product="Xerox copies (B&W)",
        currency="INR",
        base_cost_paise=product["unit_cost_paise"],
        selling_price_paise=product["price_paise"],
        platform_commission_per_page_paise=product["platform_fee_per_page_paise"],
        delivery_fee_paise=product["delivery_fee_paise"],
    )


@app.get("/api/pricing")
def pricing():
    qr_enabled = os.environ.get("RAZORPAY_QR_ENABLED", "false").strip().lower() == "true"
    try:
        settings = razorpay_settings()
    except RazorpayError as error:
        return jsonify(
            locations=LOCATIONS,
            payment_configured=False,
            payment_mode=None,
            configuration_error=str(error),
            qr_payment_configured=False,
            qr_payment_configuration_error=str(error),
            route_enabled=False,
        )
    route_enabled = os.environ.get("RAZORPAY_ROUTE_ENABLED", "false").lower() == "true"
    return jsonify(
        locations=LOCATIONS,
        payment_configured=settings is not None,
        payment_mode=settings["mode"] if settings else None,
        configuration_error=None if settings else
            "Online payments are not configured. Add Razorpay test-mode keys to the backend .env file.",
        qr_payment_configured=settings is not None and qr_enabled,
        qr_payment_configuration_error=(
            None if settings is not None and qr_enabled
            else "Razorpay QR Code payments require enabled QR API access and RAZORPAY_QR_ENABLED=true."
        ),
        route_enabled=route_enabled,
        route_shop_account_configured=bool(
            os.environ.get("RAZORPAY_SHOP_LINKED_ACCOUNT_ID", "").strip()
        ),
    )


@app.errorhandler(413)
def request_entity_too_large(_error):
    return jsonify(error="The upload exceeds the 20 MB file size limit."), 413


@app.post("/api/documents")
@require_auth
def upload_document():
    uploaded = request.files.get("document")
    if uploaded is None or not uploaded.filename:
        return jsonify(error="Choose a PDF document to upload."), 400
    filename = uploaded.filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not filename or len(filename) > 200 or not filename.lower().endswith(".pdf"):
        return jsonify(error="Only PDF files with a .pdf filename are accepted."), 400

    content = uploaded.stream.read(MAX_DOCUMENT_BYTES + 1)
    if not content:
        return jsonify(error="The selected PDF is empty."), 400
    if len(content) > MAX_DOCUMENT_BYTES:
        return jsonify(error="PDF files must be 20 MB or smaller."), 413
    if b"%PDF-" not in content[:1024]:
        return jsonify(error="The selected file is not a valid PDF document."), 400
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            return jsonify(error="Password-protected PDFs are not supported."), 400
        page_count = len(reader.pages)
    except (PdfReadError, ValueError, OSError):
        return jsonify(error="This PDF could not be opened. Please choose a valid PDF."), 400
    if not 1 <= page_count <= 1000:
        return jsonify(error="PDF documents must contain between 1 and 1,000 pages."), 400

    safe_filename = "".join(
        character for character in filename if character.isprintable() and character not in "\r\n"
    )
    if not safe_filename:
        return jsonify(error="The PDF filename is invalid."), 400
    document_key = secrets.token_hex(24)
    document_path = PRIVATE_DOCUMENTS / f"{document_key}.pdf"
    try:
        PRIVATE_DOCUMENTS.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(PRIVATE_DOCUMENTS, 0o700)
        with document_path.open("xb") as document_file:
            document_file.write(content)
        if os.name != "nt":
            os.chmod(document_path, 0o600)
        cursor = get_db().execute(
            """INSERT INTO order_documents
               (document_key, customer_id, original_filename, byte_size, page_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (document_key, session["user_id"], safe_filename, len(content), page_count, now()),
        )
    except OSError:
        document_path.unlink(missing_ok=True)
        return jsonify(error="The document could not be saved. Please try again."), 500
    except sqlite3.Error:
        document_path.unlink(missing_ok=True)
        raise
    return jsonify(
        document={
            "id": cursor.lastrowid,
            "filename": safe_filename,
            "size_bytes": len(content),
            "page_count": page_count,
        }
    ), 201


@app.delete("/api/documents/<int:document_id>")
@require_auth
def remove_document(document_id):
    connection = get_db()
    document = connection.execute(
        """SELECT document_key FROM order_documents
           WHERE id = ? AND customer_id = ? AND order_id IS NULL""",
        (document_id, session["user_id"]),
    ).fetchone()
    if document is None:
        return jsonify(error="This uploaded document cannot be removed."), 404
    document_path = PRIVATE_DOCUMENTS / f"{document['document_key']}.pdf"
    try:
        document_path.unlink(missing_ok=True)
    except OSError:
        return jsonify(error="The uploaded document could not be removed. Please retry."), 500
    connection.execute("DELETE FROM order_documents WHERE id = ?", (document_id,))
    return jsonify(ok=True)


@app.get("/api/orders/<int:order_id>/document")
@require_auth
def download_order_document(order_id):
    connection = get_db()
    document = connection.execute(
        """SELECT documents.document_key, documents.original_filename
           FROM order_documents AS documents
           JOIN orders ON orders.id = documents.order_id
           WHERE orders.id = ? AND orders.customer_id = ?
             AND documents.customer_id = orders.customer_id""",
        (order_id, session["user_id"]),
    ).fetchone()
    if document is None:
        return jsonify(error="Document not found."), 404
    document_path = PRIVATE_DOCUMENTS / f"{document['document_key']}.pdf"
    if not document_path.is_file():
        return jsonify(error="The stored document is unavailable. Contact support."), 404
    response = send_file(
        document_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=document["original_filename"],
        conditional=False,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/api/availability")
@require_auth
def set_availability():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("available"), bool):
        return jsonify(error="Availability must be true or false."), 400
    connection = get_db()
    user_id = session["user_id"]
    if data["available"]:
        active = connection.execute(
            """SELECT 1 FROM orders WHERE delivery_user_id = ?
               AND status NOT IN ('COMPLETED', 'CANCELLED') LIMIT 1""",
            (user_id,),
        ).fetchone()
        if active:
            return jsonify(error="Finish your active delivery before going available."), 409
    connection.execute("UPDATE users SET available = ? WHERE id = ?", (int(data["available"]), user_id))
    user = connection.execute(
        "SELECT id, name, email, available FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return jsonify(user=user_json(user))


@app.get("/api/orders")
@require_auth
def list_orders():
    connection = get_db()
    user = current_user()
    customer_rows = connection.execute(
        """SELECT * FROM orders WHERE customer_id = ? AND payment_method = 'online'
           ORDER BY id DESC LIMIT 20""",
        (user["id"],),
    ).fetchall()
    active_rows = connection.execute(
        """SELECT * FROM orders WHERE delivery_user_id = ?
           AND payment_method = 'online'
           AND status NOT IN ('COMPLETED', 'CANCELLED') ORDER BY id DESC""",
        (user["id"],),
    ).fetchall()
    open_rows = []
    if user["available"]:
        open_rows = connection.execute(
            """SELECT * FROM orders WHERE status = 'PLACED'
               AND payment_status = 'captured'
               AND customer_id != ? AND delivery_user_id IS NULL ORDER BY id""",
            (user["id"],),
        ).fetchall()
    return jsonify(
        customer_orders=[order_json(connection, row, True, True, True) for row in customer_rows],
        active_deliveries=[order_json(connection, row, True) for row in active_rows],
        available_orders=[order_json(connection, row) for row in open_rows],
    )


@app.post("/api/orders")
@require_auth
def create_order():
    data = request.get_json(silent=True) or {}
    payment_flow = data.get("payment_flow", "checkout")
    if payment_flow not in ("checkout", "qr"):
        return jsonify(error="Choose a supported payment method."), 400
    expected_total_paise = data.get("expected_total_paise")
    if expected_total_paise is not None and (
        isinstance(expected_total_paise, bool)
        or not isinstance(expected_total_paise, int)
        or expected_total_paise < 0
    ):
        return jsonify(error="The displayed checkout total is invalid. Refresh and retry."), 400
    try:
        settings = razorpay_settings()
    except RazorpayError as error:
        return jsonify(error=str(error)), 503
    if settings is None:
        return jsonify(
            error="Online payments are not configured. Add Razorpay test-mode keys to the backend .env file."
        ), 503
    if (
        payment_flow == "qr"
        and os.environ.get("RAZORPAY_QR_ENABLED", "false").strip().lower() != "true"
    ):
        return jsonify(
            error="Razorpay QR Code payments are disabled. Enable QR Code payments for your merchant account and set RAZORPAY_QR_ENABLED=true."
        ), 503
    idempotency_key = request.headers.get("X-Idempotency-Key", "").strip()
    if not 8 <= len(idempotency_key) <= 128:
        return jsonify(error="A valid payment request key is required. Please retry checkout."), 400
    try:
        request_fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        return jsonify(error="The order request is invalid."), 400
    prior = get_db().execute(
        "SELECT * FROM orders WHERE customer_id = ? AND idempotency_key = ?",
        (session["user_id"], idempotency_key),
    ).fetchone()
    if prior is not None:
        if prior["request_fingerprint"] != request_fingerprint:
            return jsonify(
                error="This payment request key was already used for a different order. Refresh checkout and retry."
            ), 409
        if prior["payment_status"] == "captured":
            return jsonify(
                order=order_json(get_db(), prior, True, True),
                payment_checkout=None,
                payment_error=None,
            ), 200
        try:
            if payment_flow == "qr":
                payment_qr = start_qr_payment(get_db(), prior)
                payment_checkout = None
            else:
                payment_checkout = start_gateway_order(get_db(), prior)
                payment_qr = None
        except RazorpayError as error:
            return jsonify(
                order=order_json(get_db(), prior, True, True, True),
                payment_checkout=None,
                payment_qr=None,
                payment_error=str(error),
            ), 200
        return jsonify(
            order=order_json(get_db(), prior, True, True, True),
            payment_checkout=payment_checkout,
            payment_qr=payment_qr,
            payment_error=None,
        ), 200
    destination = data.get("destination")
    instructions = str(data.get("instructions", "")).strip()
    items = data.get("items")
    if destination not in LOCATIONS:
        return jsonify(error="Choose one of the listed campus delivery locations."), 400
    if data.get("payment_method", "online") != "online":
        return jsonify(error="Only online payment is supported."), 400
    if len(instructions) > 300:
        return jsonify(error="Delivery instructions must be 300 characters or fewer."), 400
    if not isinstance(items, list) or not items or len(items) > 20:
        return jsonify(error="Add at least one item to your order."), 400

    connection = get_db()
    selected_items = []
    for item in items:
        if not isinstance(item, dict):
            return jsonify(error="An order item is invalid."), 400
        try:
            product_id = int(item.get("product_id"))
            if not isinstance(item.get("quantity", 1), bool):
                quantity = int(item.get("quantity", 1))
            else:
                raise ValueError
            copies = int(item.get("copies", 0))
        except (TypeError, ValueError):
            return jsonify(error="An order item is invalid."), 400
        page_range = item.get("page_range", "all")
        duplex = item.get("duplex", False)
        if not isinstance(duplex, bool):
            return jsonify(error="Choose single-sided or double-sided printing."), 400
        product = connection.execute(
            """SELECT id, name, price_paise, pricing_type, delivery_fee_paise,
                      platform_fee_paise, platform_fee_per_page_paise
               FROM products WHERE id = ? AND active = 1""",
            (product_id,),
        ).fetchone()
        if product is None:
            return jsonify(error="One of the selected items is no longer available."), 400
        if product["pricing_type"] == "page":
            if not 1 <= copies <= 20:
                return jsonify(error="Printing supports between 1 and 20 copies."), 400
            if not isinstance(page_range, str) or len(page_range) > 500:
                return jsonify(error="Enter a valid printing page range."), 400
            quantity = copies
        else:
            if not 1 <= quantity <= 20:
                return jsonify(error="Each quantity must be between 1 and 20."), 400
            if copies != 0 or page_range != "all" or duplex:
                return jsonify(error="Page count and copies only apply to printing services."), 400
        selected_items.append((product, quantity, 0, copies, page_range, duplex))

    has_printing = any(item[0]["pricing_type"] == "page" for item in selected_items)
    document_id = data.get("document_id")
    connection.execute("BEGIN IMMEDIATE")
    raced_order = connection.execute(
        "SELECT * FROM orders WHERE customer_id = ? AND idempotency_key = ?",
        (session["user_id"], idempotency_key),
    ).fetchone()
    if raced_order is not None:
        connection.rollback()
        if raced_order["request_fingerprint"] != request_fingerprint:
            return jsonify(
                error="This payment request key was already used for a different order. Refresh checkout and retry."
            ), 409
        if raced_order["payment_status"] == "captured":
            return jsonify(
                order=order_json(connection, raced_order, True, True),
                payment_checkout=None,
                payment_error=None,
            ), 200
        try:
            if payment_flow == "qr":
                payment_qr = start_qr_payment(connection, raced_order)
                payment_checkout = None
            else:
                payment_checkout = start_gateway_order(connection, raced_order)
                payment_qr = None
        except RazorpayError as error:
            return jsonify(
                order=order_json(connection, raced_order, True, True, True),
                payment_checkout=None,
                payment_qr=None,
                payment_error=str(error),
            ), 200
        return jsonify(
        order=order_json(connection, raced_order, True, True, True),
        payment_checkout=payment_checkout,
        payment_qr=payment_qr,
        payment_error=None,
        ), 200
    refreshed_items = []
    for product, quantity, page_count, copies, page_range, duplex in selected_items:
        current_product = connection.execute(
            """SELECT id, name, price_paise, unit_cost_paise, pricing_type,
                      delivery_fee_paise, platform_fee_paise,
                      platform_fee_per_page_paise
               FROM products WHERE id = ? AND active = 1""",
            (product["id"],),
        ).fetchone()
        if current_product is None:
            connection.rollback()
            return jsonify(error="One of the selected items is no longer available."), 400
        refreshed_items.append(
            (current_product, quantity, page_count, copies, page_range, duplex)
        )
    selected_items = refreshed_items
    document = None
    if has_printing:
        try:
            document_id = int(document_id)
        except (TypeError, ValueError):
            connection.rollback()
            return jsonify(error="Upload a PDF document for your printing order."), 400
        document = connection.execute(
            """SELECT id, customer_id, order_id, page_count FROM order_documents
               WHERE id = ? AND customer_id = ? AND order_id IS NULL""",
            (document_id, session["user_id"]),
        ).fetchone()
        if document is None:
            connection.rollback()
            return jsonify(error="Upload a valid PDF you own before placing this order."), 400
        prepared_items = []
        for product, quantity, _page_count, copies, page_range, duplex in selected_items:
            try:
                page_count = parse_page_range(page_range, document["page_count"])
            except ValueError as error:
                connection.rollback()
                return jsonify(error=str(error)), 400
            prepared_items.append(
                (product, quantity, page_count, copies, page_range, duplex)
            )
        selected_items = prepared_items
    elif document_id is not None:
        connection.rollback()
        return jsonify(error="A document can only be attached to a printing order."), 400

    totals = calculate_order_totals(selected_items)
    if (
        expected_total_paise is not None
        and expected_total_paise != totals["total_paise"]
    ):
        connection.rollback()
        return jsonify(
            error="Pricing changed while you were checking out. Review the updated cart total and try again.",
            pricing_changed=True,
        ), 409
    status = "PAYMENT_PENDING"
    payment_status = "pending"
    created = now()
    cursor = connection.execute(
        """INSERT INTO orders
           (customer_id, destination, instructions, item_total_paise, subtotal_paise,
            delivery_fee_paise, platform_fee_paise, total_paise, status, payment_method,
            payment_status, idempotency_key, request_fingerprint, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?)""",
        (
            session["user_id"], destination, instructions, totals["item_subtotal_paise"],
            totals["item_subtotal_paise"], totals["delivery_fee_paise"],
            totals["platform_fee_paise"], totals["total_paise"], status,
            payment_status, idempotency_key, request_fingerprint, created, created,
        ),
    )
    order_id = cursor.lastrowid
    for item in totals["items"]:
        product = item["product"]
        connection.execute(
            """INSERT INTO order_items
               (order_id, product_id, product_name, quantity, unit_price_paise,
                unit_cost_paise, page_count, copies, billable_pages, page_range, duplex)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id, product["id"], product["name"], item["quantity"],
                product["price_paise"], item["unit_cost_paise"], item["page_count"], item["copies"],
                item["billable_pages"], item["page_range"], int(item["duplex"]),
            ),
        )
    if document is not None:
        attached = connection.execute(
            """UPDATE order_documents SET order_id = ?, attached_at = ?
               WHERE id = ? AND customer_id = ? AND order_id IS NULL""",
            (order_id, created, document["id"], session["user_id"]),
        )
        if attached.rowcount != 1:
            connection.rollback()
            return jsonify(error="This PDF is already attached to another order."), 409
    add_history(connection, order_id, status, session["user_id"])
    connection.executemany(
        """INSERT INTO payment_allocations
           (order_id, recipient_type, amount_paise, settlement_status, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (order_id, "shop", totals["item_subtotal_paise"], "awaiting_payment", created),
            (order_id, "runner", totals["delivery_fee_paise"], "pending_assignment", created),
            (order_id, "platform", totals["platform_fee_paise"], "awaiting_payment", created),
        ],
    )
    connection.commit()
    row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    try:
        if payment_flow == "qr":
            payment_qr = start_qr_payment(connection, row)
            payment_checkout = None
        else:
            payment_checkout = start_gateway_order(connection, row)
            payment_qr = None
        payment_error = None
    except RazorpayError as error:
        payment_checkout = None
        payment_qr = None
        payment_error = str(error)
    return jsonify(
        order=order_json(connection, row, True, True, True),
        payment_checkout=payment_checkout,
        payment_qr=payment_qr,
        payment_error=payment_error,
    ), 201


@app.post("/api/orders/<int:order_id>/payment")
@require_auth
def retry_payment(order_id):
    connection = get_db()
    order = connection.execute(
        "SELECT * FROM orders WHERE id = ? AND customer_id = ?", (order_id, session["user_id"])
    ).fetchone()
    if order is None:
        return jsonify(error="Order not found."), 404
    if order["payment_status"] == "captured":
        return jsonify(error="This order has already been paid."), 409
    if order["status"] not in ("PAYMENT_PENDING", "CANCELLED"):
        return jsonify(error="This order is not awaiting payment."), 409
    try:
        payment_checkout = start_gateway_order(connection, order)
    except RazorpayError as error:
        return jsonify(error=str(error)), 503
    return jsonify(payment_checkout=payment_checkout), 200


@app.post("/api/orders/<int:order_id>/payment-qr")
@require_auth
def retry_qr_payment(order_id):
    connection = get_db()
    order = connection.execute(
        "SELECT * FROM orders WHERE id = ? AND customer_id = ?",
        (order_id, session["user_id"]),
    ).fetchone()
    if order is None:
        return jsonify(error="Order not found."), 404
    if order["payment_status"] == "captured":
        return jsonify(error="This order has already been paid."), 409
    if order["status"] not in ("PAYMENT_PENDING", "CANCELLED"):
        return jsonify(error="This order is not awaiting payment."), 409
    try:
        payment_qr = start_qr_payment(connection, order)
    except RazorpayError as error:
        return jsonify(error=str(error)), 503
    return jsonify(payment_qr=payment_qr), 200


@app.post("/api/orders/<int:order_id>/verify-qr-payment")
@require_auth
def verify_qr_payment(order_id):
    connection = get_db()
    record = connection.execute(
        """SELECT payments.*, orders.customer_id
           FROM payment_records AS payments
           JOIN orders ON orders.id = payments.order_id
           WHERE payments.order_id = ? AND orders.customer_id = ?""",
        (order_id, session["user_id"]),
    ).fetchone()
    if record is None or not record["gateway_qr_code_id"]:
        return jsonify(error="QR payment was not found for this order."), 404
    if record["status"] == "captured":
        row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return jsonify(
            order=order_json(connection, row, True, True, True),
            payment_status="captured",
        ), 200
    try:
        collection = razorpay_request(
            "GET",
            f"/payments/qr_codes/{record['gateway_qr_code_id']}/payments?count=10",
        )
    except RazorpayError as error:
        return jsonify(error=str(error), payment_status="pending"), 503
    items = collection.get("items")
    if not isinstance(items, list):
        return jsonify(error="Razorpay returned an invalid QR payment list."), 502
    for payment in items:
        if (
            not isinstance(payment, dict)
            or not isinstance(payment.get("id"), str)
            or payment.get("amount") != record["amount_paise"]
            or payment.get("currency") != "INR"
        ):
            continue
        if payment.get("status") == "captured":
            try:
                verified_payment = razorpay_request("GET", f"/payments/{payment['id']}")
            except RazorpayError as error:
                return jsonify(error=str(error), payment_status="pending"), 503
            if (
                verified_payment.get("id") != payment["id"]
                or verified_payment.get("amount") != record["amount_paise"]
                or verified_payment.get("currency") != "INR"
                or verified_payment.get("status") != "captured"
                or (
                    verified_payment.get("qr_code_id")
                    and verified_payment["qr_code_id"] != record["gateway_qr_code_id"]
                )
            ):
                return jsonify(error="Payment details do not match this order's QR code."), 409
            verified_payment["qr_code_id"] = record["gateway_qr_code_id"]
            if not mark_payment_captured(connection, verified_payment, "qr_api_verified"):
                return jsonify(error="Payment could not be matched to this order."), 409
            attempt_route_transfers(connection, order_id)
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return jsonify(
                order=order_json(connection, row, True, True, True),
                payment_status="captured",
            ), 200
        if payment.get("status") == "failed":
            update_payment_failure(
                connection, record["gateway_qr_code_id"], payment["id"], "failed"
            )
    return jsonify(
        message="Razorpay has not confirmed a captured QR payment. The order remains pending.",
        payment_status="pending",
    ), 202


@app.post("/api/orders/<int:order_id>/verify-payment")
@require_auth
def verify_order_payment(order_id):
    data = request.get_json(silent=True) or {}
    gateway_order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")
    connection = get_db()
    record = connection.execute(
        """SELECT payments.*, orders.total_paise, orders.customer_id
           FROM payment_records AS payments
           JOIN orders ON orders.id = payments.order_id
           WHERE payments.order_id = ? AND orders.customer_id = ?""",
        (order_id, session["user_id"]),
    ).fetchone()
    if record is None:
        return jsonify(error="Payment order not found."), 404
    if (
        not isinstance(gateway_order_id, str)
        or not isinstance(payment_id, str)
        or not isinstance(signature, str)
        or gateway_order_id != record["gateway_order_id"]
    ):
        return jsonify(error="The payment response is invalid. No order was confirmed."), 400
    settings = razorpay_settings()
    if settings is None:
        return jsonify(error="Payment verification is not configured on the server."), 503
    expected_signature = hmac.new(
        settings["key_secret"].encode("utf-8"),
        f"{gateway_order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        return jsonify(error="Payment signature verification failed. No order was confirmed."), 400
    try:
        payment_entity = razorpay_request("GET", f"/payments/{payment_id}")
    except RazorpayError as error:
        return jsonify(error=str(error), payment_status="pending"), 503
    if (
        payment_entity.get("order_id") != gateway_order_id
        or payment_entity.get("amount") != record["amount_paise"]
        or payment_entity.get("currency") != "INR"
    ):
        return jsonify(error="Payment details do not match this order."), 400
    if payment_entity.get("status") == "captured":
        if not mark_payment_captured(connection, payment_entity, "signature_and_api_verified"):
            return jsonify(error="Payment could not be matched to this order."), 409
        attempt_route_transfers(connection, order_id)
        row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return jsonify(order=order_json(connection, row, True, True), payment_status="captured"), 200
    if payment_entity.get("status") in ("failed", "refunded"):
        if payment_entity["status"] == "failed":
            update_payment_failure(connection, gateway_order_id, payment_id, "failed")
        else:
            connection.execute(
                """UPDATE payment_records SET status = 'refunded',
                   verification_status = 'api_verified', updated_at = ?
                   WHERE order_id = ?""",
                (now(), order_id),
            )
            connection.execute(
                "UPDATE orders SET payment_status = 'refunded', updated_at = ? WHERE id = ?",
                (now(), order_id),
            )
        return jsonify(
            error=f"Razorpay reports this payment as {payment_entity['status']}.",
            payment_status=payment_entity["status"],
        ), 409
    return jsonify(
        message="Payment is still being verified. The order will be released only after Razorpay confirms capture.",
        payment_status=payment_entity.get("status", "pending"),
    ), 202


@app.post("/api/orders/<int:order_id>/payment-cancelled")
@require_auth
def payment_cancelled(order_id):
    connection = get_db()
    cursor = connection.execute(
        """UPDATE payment_records SET status = 'cancelled', updated_at = ?
           WHERE order_id = ? AND status != 'captured'
             AND order_id IN (SELECT id FROM orders WHERE customer_id = ?)""",
        (now(), order_id, session["user_id"]),
    )
    if cursor.rowcount != 1:
        return jsonify(error="A pending payment was not found for this order."), 404
    connection.execute(
        """UPDATE orders SET payment_status = 'cancelled', updated_at = ?
           WHERE id = ? AND customer_id = ? AND status = 'PAYMENT_PENDING'""",
        (now(), order_id, session["user_id"]),
    )
    return jsonify(payment_status="cancelled")


@app.post("/api/orders/<int:order_id>/payment-failed")
@require_auth
def payment_failed(order_id):
    data = request.get_json(silent=True) or {}
    payment_id = data.get("razorpay_payment_id")
    if not isinstance(payment_id, str) or not payment_id:
        return jsonify(error="Razorpay payment reference is missing."), 400
    connection = get_db()
    record = connection.execute(
        """SELECT payments.gateway_order_id, payments.amount_paise
           FROM payment_records AS payments JOIN orders ON orders.id = payments.order_id
           WHERE payments.order_id = ? AND orders.customer_id = ?""",
        (order_id, session["user_id"]),
    ).fetchone()
    if record is None:
        return jsonify(error="Payment order not found."), 404
    try:
        entity = razorpay_request("GET", f"/payments/{payment_id}")
    except RazorpayError as error:
        return jsonify(error=str(error), payment_status="pending"), 503
    if (
        entity.get("id") != payment_id
        or entity.get("order_id") != record["gateway_order_id"]
        or entity.get("amount") != record["amount_paise"]
        or entity.get("currency") != "INR"
    ):
        return jsonify(error="Payment details do not match this order."), 400
    if entity.get("status") == "captured":
        if not mark_payment_captured(connection, entity, "api_verified"):
            return jsonify(error="Captured payment could not be matched to this order."), 409
        attempt_route_transfers(connection, order_id)
        row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return jsonify(order=order_json(connection, row, True, True), payment_status="captured"), 200
    if entity.get("status") != "failed":
        return jsonify(
            message="Razorpay has not confirmed a failed payment. The order remains pending.",
            payment_status=entity.get("status", "pending"),
        ), 202
    if not update_payment_failure(connection, record["gateway_order_id"], payment_id, "failed"):
        return jsonify(error="Failed payment could not be matched to this order."), 409
    row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return jsonify(order=order_json(connection, row, True, True), payment_status="failed"), 200


@app.post("/api/webhooks/razorpay")
def razorpay_webhook():
    webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        return jsonify(error="Razorpay webhook verification is not configured."), 503
    raw_body = request.get_data(cache=False)
    if not verify_webhook_signature(
        raw_body, request.headers.get("X-Razorpay-Signature", ""), webhook_secret
    ):
        return jsonify(error="Invalid Razorpay webhook signature."), 400
    event_id = request.headers.get("X-Razorpay-Event-Id", "").strip()
    if not event_id or len(event_id) > 200:
        return jsonify(error="Razorpay webhook event ID is required."), 400
    try:
        body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify(error="Invalid Razorpay webhook payload."), 400
    if not isinstance(body, dict):
        return jsonify(error="Invalid Razorpay webhook payload."), 400
    event_type = body.get("event")
    if not isinstance(event_type, str):
        return jsonify(error="Razorpay webhook event type is missing."), 400
    connection = get_db()
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    existing_event = connection.execute(
        """SELECT event_id, processing_status FROM payment_webhook_events
           WHERE event_id = ? OR payload_sha256 = ?""",
        (event_id, payload_hash),
    ).fetchone()
    if existing_event and existing_event["processing_status"] != "failed":
        connection.rollback()
        return jsonify(ok=True, duplicate=True)
    stamp = now()
    if existing_event:
        event_db_id = existing_event["event_id"]
        connection.execute(
            """UPDATE payment_webhook_events SET processing_status = 'processing',
               event_type = ?, received_at = ?, processed_at = NULL WHERE event_id = ?""",
            (event_type, stamp, event_db_id),
        )
    else:
        event_db_id = event_id
        connection.execute(
            """INSERT INTO payment_webhook_events
               (event_id, event_type, payload_sha256, processing_status, received_at)
               VALUES (?, ?, ?, 'processing', ?)""",
            (event_db_id, event_type, payload_hash, stamp),
        )
    connection.commit()

    def reject(message, status):
        connection.execute(
            """UPDATE payment_webhook_events SET processing_status = 'failed'
               WHERE event_id = ?""",
            (event_db_id,),
        )
        connection.commit()
        return jsonify(error=message), status

    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        return reject("Invalid Razorpay webhook payload.", 400)
    if event_type in ("payment.captured", "payment.authorized", "payment.failed"):
        entity = payload.get("payment", {}).get("entity", {})
        if not isinstance(entity, dict):
            return reject("Invalid payment webhook entity.", 400)
        if (
            not (
                isinstance(entity.get("order_id"), str)
                or isinstance(entity.get("qr_code_id"), str)
            )
            or not isinstance(entity.get("amount"), int)
            or entity.get("currency") != "INR"
        ):
            return reject("Payment event is missing valid order or QR amount details.", 400)
        if event_type == "payment.captured":
            if entity.get("status") != "captured":
                return reject("Captured payment event has an unexpected status.", 400)
            if not mark_payment_captured(connection, entity, "webhook_verified"):
                return reject("Payment event could not be matched to a pending order.", 409)
            linked_order = connection.execute(
                "SELECT order_id FROM payment_records WHERE gateway_payment_id = ?",
                (entity.get("id"),),
            ).fetchone()
            if linked_order:
                attempt_route_transfers(connection, linked_order["order_id"])
        elif event_type == "payment.failed":
            if entity.get("status") != "failed":
                return reject("Failed payment event has an unexpected status.", 400)
            if not update_payment_failure(
                connection,
                entity.get("qr_code_id") or entity.get("order_id"),
                entity.get("id"),
                "failed",
            ):
                return reject("Failed payment event could not be matched.", 409)
        else:
            if entity.get("status") not in ("authorized", "captured"):
                return reject("Authorized payment event has an unexpected status.", 400)
            if entity.get("status") == "authorized":
                connection.execute(
                    """UPDATE payment_records SET status = 'authorized',
                       verification_status = 'webhook_verified', updated_at = ?
                       WHERE (gateway_order_id = ? OR gateway_qr_code_id = ?)
                         AND status != 'captured'""",
                    (
                        now(),
                        entity.get("order_id"),
                        entity.get("qr_code_id"),
                    ),
                )
                connection.execute(
                    """UPDATE orders SET payment_status = 'authorized', updated_at = ?
                       WHERE id IN (
                           SELECT order_id FROM payment_records
                           WHERE gateway_order_id = ? OR gateway_qr_code_id = ?
                       )
                         AND status = 'PAYMENT_PENDING'""",
                    (
                        now(),
                        entity.get("order_id"),
                        entity.get("qr_code_id"),
                    ),
                )
    elif event_type.startswith("transfer."):
        transfer = payload.get("transfer", {}).get("entity", {})
        if not isinstance(transfer, dict):
            return reject("Invalid transfer webhook entity.", 400)
        transfer_id = transfer.get("id")
        status_by_event = {
            "transfer.processed": "processed",
            "transfer.failed": "failed",
            "transfer.reversed": "reversed",
        }
        transfer_status = status_by_event.get(event_type)
        if not transfer_id or transfer_status is None:
            return reject("Transfer event is missing its transfer reference.", 400)
        transfer_row = connection.execute(
            """SELECT order_id, recipient_type FROM payment_transfers
               WHERE gateway_transfer_id = ?""",
            (transfer_id,),
        ).fetchone()
        if transfer_row is not None:
            connection.execute(
                "UPDATE payment_transfers SET status = ?, updated_at = ? WHERE gateway_transfer_id = ?",
                (transfer_status, now(), transfer_id),
            )
            connection.execute(
                """UPDATE payment_allocations SET settlement_status = ?
                   WHERE order_id = ? AND recipient_type = ?""",
                (transfer_status, transfer_row["order_id"], transfer_row["recipient_type"]),
            )
    elif event_type in (
        "refund.created",
        "refund.processed",
        "refund.failed",
        "refund.speed_changed",
    ):
        refund = payload.get("refund", {}).get("entity", {})
        if not isinstance(refund, dict):
            return reject("Invalid refund webhook entity.", 400)
        payment_id = refund.get("payment_id")
        refund_id = refund.get("id")
        amount = refund.get("amount")
        expected_statuses = {
            "refund.created": {"created", "pending", "processed"},
            "refund.processed": {"processed"},
            "refund.failed": {"failed"},
            "refund.speed_changed": {"processed"},
        }
        payment = connection.execute(
            "SELECT order_id, amount_paise, refunded_paise FROM payment_records WHERE gateway_payment_id = ?",
            (payment_id,),
        ).fetchone()
        refund_status = refund.get("status")
        if (
            not payment
            or not refund_id
            or not isinstance(amount, int)
            or amount <= 0
            or refund.get("currency") != "INR"
            or refund_status not in expected_statuses[event_type]
        ):
            return reject("Refund event could not be matched to a payment.", 409)
        previous_refund = connection.execute(
            "SELECT amount_paise, status FROM payment_refunds WHERE gateway_refund_id = ?",
            (refund_id,),
        ).fetchone()
        if previous_refund and previous_refund["amount_paise"] != amount:
            return reject("Refund amount changed for an existing refund reference.", 409)
        will_process_refund = refund_status == "processed" and (
            previous_refund is None or previous_refund["status"] != "processed"
        )
        refunded_total = payment["refunded_paise"] + amount if will_process_refund else payment["refunded_paise"]
        if will_process_refund and refunded_total > payment["amount_paise"]:
            return reject("Refund total exceeds the captured payment amount.", 409)
        stamp = now()
        connection.execute(
            """INSERT INTO payment_refunds
               (order_id, gateway_refund_id, amount_paise, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(gateway_refund_id) DO UPDATE SET status = excluded.status,
                   updated_at = excluded.updated_at""",
            (payment["order_id"], refund_id, amount, refund_status, stamp, stamp),
        )
        if will_process_refund:
            payment_status = (
                "refunded"
                if refunded_total >= payment["amount_paise"]
                else "partially_refunded"
            )
            connection.execute(
                """UPDATE payment_records SET status = ?, refunded_paise = ?, updated_at = ?
                   WHERE order_id = ?""",
                (payment_status, refunded_total, stamp, payment["order_id"]),
            )
            connection.execute(
                "UPDATE orders SET payment_status = ?, updated_at = ? WHERE id = ?",
                (payment_status, stamp, payment["order_id"]),
            )

    connection.execute(
        """UPDATE payment_webhook_events SET processing_status = 'processed',
           processed_at = ? WHERE event_id = ?""",
        (now(), event_db_id),
    )
    connection.commit()
    return jsonify(ok=True)


@app.get("/api/admin/reconciliation")
def payment_reconciliation():
    admin_token = os.environ.get("CAMPUSGO_ADMIN_TOKEN", "").strip()
    supplied_token = request.headers.get("X-Admin-Token", "")
    if not admin_token:
        return jsonify(error="Admin reconciliation is not configured."), 503
    if not supplied_token or not hmac.compare_digest(admin_token, supplied_token):
        return jsonify(error="Not authorized."), 401
    connection = get_db()
    records = connection.execute(
        """SELECT orders.id AS order_id, orders.customer_id, orders.delivery_user_id,
                  orders.subtotal_paise AS item_subtotal_paise, orders.delivery_fee_paise,
                  orders.platform_fee_paise, orders.total_paise, orders.payment_status,
                  payments.gateway_order_id, payments.gateway_qr_code_id,
                  payments.gateway_payment_id,
                  payments.amount_paise, payments.currency, payments.status AS gateway_status,
                  payments.verification_status, payments.gateway_fee_paise,
                  payments.gateway_tax_paise
           FROM orders JOIN payment_records AS payments ON payments.order_id = orders.id
           ORDER BY orders.id DESC LIMIT 200"""
    ).fetchall()
    result = []
    for record in records:
        order_id = record["order_id"]
        row = dict(record)
        row["allocations"] = [
            dict(item)
            for item in connection.execute(
                """SELECT recipient_type, recipient_user_id, amount_paise, settlement_status
                   FROM payment_allocations WHERE order_id = ? ORDER BY id""",
                (order_id,),
            ).fetchall()
        ]
        order_items = connection.execute(
            """SELECT product_name, quantity, unit_price_paise, unit_cost_paise,
                      billable_pages, page_count, copies
               FROM order_items WHERE order_id = ? ORDER BY id""",
            (order_id,),
        ).fetchall()
        row["items"] = [
            {
                **dict(item),
                "selling_subtotal_paise": item["unit_price_paise"] * (
                    item["billable_pages"]
                    if item["billable_pages"]
                    else item["quantity"]
                ),
                "shop_cost_total_paise": item["unit_cost_paise"] * (
                    item["billable_pages"]
                    if item["billable_pages"]
                    else item["quantity"]
                ),
            }
            for item in order_items
        ]
        row["transfers"] = [
            dict(item)
            for item in connection.execute(
                """SELECT recipient_type, recipient_user_id, linked_account_id,
                          amount_paise, gateway_transfer_id, status
                   FROM payment_transfers WHERE order_id = ? ORDER BY id""",
                (order_id,),
            ).fetchall()
        ]
        row["refunds"] = [
            dict(item)
            for item in connection.execute(
                """SELECT gateway_refund_id, amount_paise, status
                   FROM payment_refunds WHERE order_id = ? ORDER BY id""",
                (order_id,),
            ).fetchall()
        ]
        result.append(row)
    return jsonify(payments=result)


@app.post("/api/admin/route-accounts")
def configure_runner_route_account():
    admin_token = os.environ.get("CAMPUSGO_ADMIN_TOKEN", "").strip()
    supplied_token = request.headers.get("X-Admin-Token", "")
    if not admin_token:
        return jsonify(error="Route account management is not configured."), 503
    if not supplied_token or not hmac.compare_digest(admin_token, supplied_token):
        return jsonify(error="Not authorized."), 401
    data = request.get_json(silent=True) or {}
    try:
        runner_id = int(data.get("runner_user_id"))
    except (TypeError, ValueError):
        return jsonify(error="Choose a valid delivery student account."), 400
    linked_account_id = data.get("linked_account_id")
    if (
        not isinstance(linked_account_id, str)
        or not re.fullmatch(r"acc_[A-Za-z0-9]+", linked_account_id)
    ):
        return jsonify(error="Enter the approved Razorpay Linked Account ID."), 400
    cursor = get_db().execute(
        "UPDATE users SET route_account_id = ? WHERE id = ?",
        (linked_account_id, runner_id),
    )
    if cursor.rowcount != 1:
        return jsonify(error="Delivery student account not found."), 404
    return jsonify(ok=True, runner_user_id=runner_id, linked_account_id=linked_account_id)


@app.post("/api/orders/<int:order_id>/accept")
@require_auth
def accept_order(order_id):
    connection = get_db()
    user_id = session["user_id"]
    connection.execute("BEGIN IMMEDIATE")
    user = connection.execute("SELECT available FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or not user["available"]:
        connection.rollback()
        return jsonify(error="Go available before accepting a delivery."), 409
    active = connection.execute(
        """SELECT 1 FROM orders WHERE delivery_user_id = ?
           AND status NOT IN ('COMPLETED', 'CANCELLED') LIMIT 1""",
        (user_id,),
    ).fetchone()
    if active:
        connection.rollback()
        return jsonify(error="Complete your current delivery before accepting another."), 409
    cursor = connection.execute(
        """UPDATE orders SET delivery_user_id = ?, status = 'ACCEPTED', updated_at = ?
           WHERE id = ? AND status = 'PLACED' AND delivery_user_id IS NULL
           AND customer_id != ? AND payment_status = 'captured'""",
        (user_id, now(), order_id, user_id),
    )
    if cursor.rowcount != 1:
        connection.rollback()
        return jsonify(error="This order is no longer available."), 409
    connection.execute("UPDATE users SET available = 0 WHERE id = ?", (user_id,))
    connection.execute(
        """UPDATE payment_allocations SET recipient_user_id = ?, settlement_status = 'pending_assignment'
           WHERE order_id = ? AND recipient_type = 'runner'""",
        (user_id, order_id),
    )
    add_history(connection, order_id, "ACCEPTED", user_id)
    connection.commit()
    attempt_route_transfers(connection, order_id)
    row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return jsonify(order=order_json(connection, row, True))


@app.post("/api/orders/<int:order_id>/status")
@require_auth
def advance_status(order_id):
    data = request.get_json(silent=True) or {}
    connection = get_db()
    connection.execute("BEGIN IMMEDIATE")
    order = connection.execute(
        "SELECT * FROM orders WHERE id = ? AND delivery_user_id = ?",
        (order_id, session["user_id"]),
    ).fetchone()
    if order is None:
        connection.rollback()
        return jsonify(error="Active delivery not found."), 404
    next_status = STATUS_FLOW.get(order["status"])
    if not next_status or data.get("status") != next_status:
        connection.rollback()
        return jsonify(error="That delivery status transition is not allowed."), 409
    connection.execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
        (next_status, now(), order_id),
    )
    if next_status == "ITEMS_COLLECTED":
        connection.execute(
            """UPDATE payment_allocations SET settlement_status = 'paid_at_shop_by_runner'
               WHERE order_id = ? AND recipient_type = 'shop'
                 AND settlement_status IN ('route_unavailable', 'linked_account_unavailable',
                     'amount_unavailable_after_gateway_fees', 'failed', 'reversed',
                     'pending_verification')""",
            (order_id,),
        )
    add_history(connection, order_id, next_status, session["user_id"])
    connection.commit()
    row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return jsonify(order=order_json(connection, row, True))


@app.post("/api/orders/<int:order_id>/confirm")
@require_auth
def confirm_delivery(order_id):
    connection = get_db()
    connection.execute("BEGIN IMMEDIATE")
    order = connection.execute(
        "SELECT * FROM orders WHERE id = ? AND customer_id = ?",
        (order_id, session["user_id"]),
    ).fetchone()
    if order is None or order["status"] != "DELIVERED" or order["payment_status"] != "captured":
        connection.rollback()
        return jsonify(error="Only a delivered order can be confirmed."), 409
    stamp = now()
    connection.execute(
        "UPDATE orders SET status = 'COMPLETED', updated_at = ? WHERE id = ?",
        (stamp, order_id),
    )
    connection.execute("UPDATE users SET available = 1 WHERE id = ?", (order["delivery_user_id"],))
    add_history(connection, order_id, "COMPLETED", session["user_id"])
    connection.commit()
    row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return jsonify(order=order_json(connection, row, True))


@app.get("/", defaults={"path": ""})
@app.get("/<path:path>")
def frontend(path):
    if FRONTEND_DIST.exists():
        target = FRONTEND_DIST / path
        if path and target.is_file():
            return send_from_directory(FRONTEND_DIST, path)
        return send_from_directory(FRONTEND_DIST, "index.html")
    return (
        "CampusGo API is running. Start the frontend with `cd frontend; npm run dev`.",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
    )


init_db()

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
