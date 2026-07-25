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
                    specs TEXT,
                    floor_price REAL
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
                    shipping_address TEXT,
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
                CREATE TABLE IF NOT EXISTS session_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    user_id TEXT,
                    messages TEXT,
                    summary TEXT,
                    msg_count INTEGER,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bargain_sessions (
                    session_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    rounds INTEGER DEFAULT 0,
                    last_offer REAL,
                    updated_at TEXT,
                    PRIMARY KEY (session_id, product_id)
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    close_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, status);
                """
            )
            # 兼容旧库：products 补 floor_price 列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
            if "floor_price" not in cols:
                conn.execute("ALTER TABLE products ADD COLUMN floor_price REAL")
            # 兼容旧库：orders 补 shipping_address 列
            ocols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
            if "shipping_address" not in ocols:
                conn.execute("ALTER TABLE orders ADD COLUMN shipping_address TEXT")
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

    def archive_session(self, session_id: str, user_id: str,
                        messages: list, summary: Optional[str]) -> None:
        """会话冷归档:把完整会话写入 session_archive(审计/离线分析,永久留存)。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO session_archive (session_id, user_id, messages, summary, "
                "msg_count, archived_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user_id, json.dumps(messages, ensure_ascii=False),
                 summary, len(messages or []), self._now()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_archived_session(self, session_id: str) -> Optional[dict]:
        """取该会话最近一条归档(测试/查询用)。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM session_archive WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_recent_archives(self, limit: int = 50) -> list[dict]:
        """H3 离线合成入口用:按 id DESC 取近 N 条会话归档,messages json.loads 成 list。

        坏 JSON（messages 字段无法解析）的记录直接跳过，不让单条脏数据崩离线脚本。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM session_archive ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["messages"] = json.loads(item["messages"]) if item["messages"] else []
                except (json.JSONDecodeError, TypeError):
                    continue
                results.append(item)
            return results
        finally:
            conn.close()

    def set_shipping_address(self, order_id: str, address: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE orders SET shipping_address = ? WHERE order_id = ?",
                (address, order_id),
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

    # ---------- 议价状态 ----------
    def get_bargain_state(self, session_id: str, product_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT rounds, last_offer FROM bargain_sessions "
                "WHERE session_id = ? AND product_id = ?",
                (session_id, product_id),
            ).fetchone()
            return {"rounds": row["rounds"], "last_offer": row["last_offer"]} if row else None
        finally:
            conn.close()

    def bump_bargain_state(self, session_id: str, product_id: str, offer: float) -> None:
        conn = self.connect()
        try:
            now = self._now()
            conn.execute(
                """INSERT INTO bargain_sessions (session_id, product_id, rounds, last_offer, updated_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(session_id, product_id)
                   DO UPDATE SET rounds = rounds + 1, last_offer = ?, updated_at = ?""",
                (session_id, product_id, offer, now, offer, now),
            )
            conn.commit()
        finally:
            conn.close()

    def clear_bargain_state(self, session_id: str) -> None:
        conn = self.connect()
        try:
            conn.execute("DELETE FROM bargain_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    # ---------- 会话生命周期(服务端签发 conversation_id + open/closed 状态机) ----------
    def create_conversation(self, user_id: str) -> dict:
        import uuid
        cid = "c-" + uuid.uuid4().hex[:16]
        now = datetime.now().isoformat(timespec="seconds")
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO conversations (conversation_id, user_id, status, created_at) "
                "VALUES (?, ?, 'open', ?)", (cid, user_id, now))
            conn.commit()
        finally:
            conn.close()
        return {"conversation_id": cid, "user_id": user_id, "status": "open", "created_at": now}

    def get_conversation(self, conversation_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?",
                               (conversation_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def close_conversation(self, conversation_id: str, reason: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE conversations SET status='closed', closed_at=?, close_reason=? "
                "WHERE conversation_id = ? AND status='open'",
                (datetime.now().isoformat(timespec="seconds"), reason, conversation_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def latest_open_conversation(self, user_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? AND status='open' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_conversations(self, user_id: str, limit: int = 20) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (user_id, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
