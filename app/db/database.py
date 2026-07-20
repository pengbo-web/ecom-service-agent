"""真实数据层：SQLite 连接、建表与读写。"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config.settings import settings


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.db_path

    def connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    stock INTEGER,
                    description TEXT,
                    specs TEXT
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user TEXT,
                    status TEXT,
                    total REAL,
                    created_at TEXT,
                    shipped_at TEXT,
                    tracking_number TEXT,
                    carrier TEXT,
                    estimated_delivery TEXT,
                    delivered_at TEXT,
                    refund_reason TEXT,
                    refund_status TEXT,
                    refund_requested_at TEXT
                );
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    name TEXT,
                    sku TEXT,
                    quantity INTEGER,
                    price REAL
                );
                CREATE TABLE IF NOT EXISTS shipments (
                    tracking_number TEXT PRIMARY KEY,
                    carrier TEXT,
                    status TEXT
                );
                CREATE TABLE IF NOT EXISTS logistics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    seq INTEGER,
                    time TEXT,
                    location TEXT,
                    description TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
                CREATE INDEX IF NOT EXISTS idx_events_tn ON logistics_events(tracking_number);
                """
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 读方法 ----------
    def _order_from_row(self, conn, row) -> dict:
        order = dict(row)
        items = conn.execute(
            "SELECT name, sku, quantity, price FROM order_items WHERE order_id = ? ORDER BY id",
            (order["order_id"],),
        ).fetchall()
        order["items"] = [dict(i) for i in items]
        return order

    def get_order(self, order_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            return self._order_from_row(conn, row) if row else None
        finally:
            conn.close()

    def list_orders(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()
            return [self._order_from_row(conn, r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _product_from_row(row) -> dict:
        p = dict(row)
        p["specs"] = json.loads(p["specs"]) if p.get("specs") else {}
        return p

    def get_product(self, product_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()
            return self._product_from_row(row) if row else None
        finally:
            conn.close()

    def all_products(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM products").fetchall()
            return [self._product_from_row(r) for r in rows]
        finally:
            conn.close()

    def get_logistics(self, tracking_number: str) -> Optional[dict]:
        conn = self.connect()
        try:
            ship = conn.execute(
                "SELECT * FROM shipments WHERE tracking_number = ?", (tracking_number,)
            ).fetchone()
            if not ship:
                return None
            events = conn.execute(
                "SELECT time, location, description FROM logistics_events "
                "WHERE tracking_number = ? ORDER BY seq",
                (tracking_number,),
            ).fetchall()
            return {
                "tracking_number": ship["tracking_number"],
                "carrier": ship["carrier"],
                "status": ship["status"],
                "events": [dict(e) for e in events],
            }
        finally:
            conn.close()

    # ---------- 写方法 ----------
    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def set_refund(self, order_id: str, reason: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                """UPDATE orders
                   SET status = 'refund_processing', refund_status = '审核中',
                       refund_reason = ?, refund_requested_at = ?
                   WHERE order_id = ?""",
                (reason, self._now(), order_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update_order_status(self, order_id: str, status: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update_stock(self, product_id: str, stock: int) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE products SET stock = ? WHERE product_id = ?", (stock, product_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
