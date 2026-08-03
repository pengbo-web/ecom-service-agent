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
                    name TEXT,
                    member_level TEXT DEFAULT 'normal'
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
                CREATE TABLE IF NOT EXISTS session_snapshots (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    messages TEXT,
                    summary TEXT,
                    updated_at TEXT
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
                CREATE TABLE IF NOT EXISTS skill_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT,
                    skill_name TEXT NOT NULL,
                    tool_calls TEXT,
                    outcome TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_skill_traces_name
                    ON skill_traces(skill_name, id);
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
            # 兼容旧库：users 补 member_level 列(券资格:会员等级)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "member_level" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN member_level TEXT DEFAULT 'normal'")
                conn.commit()
            # 兼容旧库：conversations 补 updated_at 列(最后活跃时间,供工作台排序/显示)
            ccols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
            if "updated_at" not in ccols:
                conn.execute("ALTER TABLE conversations ADD COLUMN updated_at TEXT")
                conn.execute("UPDATE conversations SET updated_at = created_at WHERE updated_at IS NULL")
                conn.commit()
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

    def create_order(self, user: str, items: list[dict], total: float,
                     status: str = "pending", shipping_address: str = "") -> dict:
        """自助下单:把用户在商城/商品卡点『立即购买』的商品写入订单库。
        items: [{name, sku, quantity, price}]。返回新建订单(含 items),供前端与
        list_user_orders 读取。order_id 格式 ORD-YYYYMMDD-XXXX(日期+随机后缀)。"""
        import uuid
        now = self._now()
        oid = f"ORD-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO orders (order_id, user, status, total, created_at, shipping_address) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (oid, user, status, total, now, shipping_address),
            )
            conn.executemany(
                "INSERT INTO order_items (order_id, name, sku, quantity, price) VALUES (?, ?, ?, ?, ?)",
                [(oid, it.get("name"), it.get("sku", ""), it.get("quantity", 1), it.get("price", 0))
                 for it in items],
            )
            conn.commit()
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (oid,)).fetchone()
            return self._order_from_row(conn, row)
        finally:
            conn.close()

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

    # ---------- Skill 执行轨迹(G2:每轮"加载了哪个 skill/调了哪些工具/结局如何") ----------
    def record_skill_trace(self, session_id: str, user_id: str, skill_name: str,
                           tool_calls: list[dict], outcome: str) -> None:
        """记录一轮 skill 执行轨迹。供 G3 按真实轨迹采集失败案例、G4 门禁分析。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
                "outcome, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user_id, skill_name,
                 json.dumps(tool_calls or [], ensure_ascii=False), outcome, self._now()),
            )
            conn.commit()
        finally:
            conn.close()

    def list_skill_traces(self, skill_name: Optional[str] = None,
                          outcomes: Optional[list[str]] = None,
                          limit: int = 200) -> list[dict]:
        """按 id DESC 取轨迹;可按 skill 名与结局过滤。tool_calls 反序列化成 list,
        坏 JSON 的行跳过(与 list_recent_archives 同口径,单条脏数据不崩离线脚本)。"""
        sql = "SELECT * FROM skill_traces"
        clauses: list[str] = []
        params: list = []
        if skill_name:
            clauses.append("skill_name = ?")
            params.append(skill_name)
        if outcomes:
            clauses.append(f"outcome IN ({','.join('?' * len(outcomes))})")
            params.extend(outcomes)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["tool_calls"] = json.loads(item["tool_calls"]) if item["tool_calls"] else []
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
        """取该用户最近**活跃**的 open 会话(按最后消息时间),登录即续上上次聊到的那条,
        而非最新创建的空会话——保证历史连续可见。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? AND status='open' "
                "ORDER BY COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT 1",
                (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def latest_conversation(self, user_id: str) -> Optional[dict]:
        """取该用户最近活跃的会话(**任意状态**),作为其唯一"规范会话"——
        单一连续会话模型下,登录/发消息都复用它(关了就重开),永不碎片化。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? "
                "ORDER BY COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT 1",
                (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def reopen_conversation(self, conversation_id: str) -> None:
        """重开一条已关闭的会话(单一连续会话:不新建,续用同一条)。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE conversations SET status='open', closed_at=NULL, close_reason=NULL "
                "WHERE conversation_id=?", (conversation_id,))
            conn.commit()
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

    def list_all_conversations(self, limit: int = 50) -> list[dict]:
        """跨用户列会话:进行中(open)优先,再按最后活跃时间倒序。供坐席工作台聚合。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations "
                "ORDER BY (status='open') DESC, COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def touch_conversation(self, conversation_id: str) -> None:
        """更新会话最后活跃时间(每轮对话/人工回复时调用),供工作台排序与显示。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (datetime.now().isoformat(timespec="seconds"), conversation_id))
            conn.commit()
        finally:
            conn.close()

    # ---------- 用户(存在性校验:先创建才可用) ----------
    def get_user(self, user_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT user_id, name, member_level FROM users WHERE user_id = ?",
                (user_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["member_level"] = d.get("member_level") or "normal"
            return d
        finally:
            conn.close()

    def create_user(self, user_id: str, name: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO users (user_id, name) VALUES (?, ?)",
                (user_id, name))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def count_user_orders(self, user_id: str) -> int:
        conn = self.connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM orders WHERE user = ?",
                               (user_id,)).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def set_member_level(self, user_id: str, level: str) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE users SET member_level = ? WHERE user_id = ?",
                         (level, user_id))
            conn.commit()
        finally:
            conn.close()

    # ---------- 会话冷快照(S1:热会话过期后历史回显兜底,与审计用 session_archive 分离) ----------
    def upsert_session_snapshot(self, session_id: str, user_id: str,
                                messages: list, summary) -> None:
        """会话冷快照:每会话恒一行最新态(upsert)。热会话过期后供历史回显兜底。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO session_snapshots (session_id, user_id, messages, summary, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "user_id=excluded.user_id, messages=excluded.messages, "
                "summary=excluded.summary, updated_at=excluded.updated_at",
                (session_id, user_id, json.dumps(messages or [], ensure_ascii=False),
                 summary, self._now()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session_snapshot(self, session_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT session_id, user_id, messages, summary, updated_at "
                "FROM session_snapshots WHERE session_id = ?", (session_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["messages"] = json.loads(d["messages"]) if d["messages"] else []
            except (ValueError, TypeError):
                d["messages"] = []
            return d
        finally:
            conn.close()

    def delete_session_snapshot(self, session_id: str) -> None:
        """删除某会话冷快照(重置对话时清历史,避免过期回显残留旧消息)。"""
        conn = self.connect()
        try:
            conn.execute("DELETE FROM session_snapshots WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()
