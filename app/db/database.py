"""真实数据层：SQLite 连接、建表与读写。"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)


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
                CREATE TABLE IF NOT EXISTS skill_canaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name TEXT NOT NULL,
                    candidate_path TEXT NOT NULL,
                    percent INTEGER NOT NULL,
                    risk TEXT,
                    policy TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_skill_canaries_active
                    ON skill_canaries(skill_name, status);
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload TEXT,
                    source_agent TEXT,
                    target_agent TEXT NOT NULL,
                    correlation_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_target
                    ON agent_events(target_agent, status, id);
                CREATE INDEX IF NOT EXISTS idx_agent_events_corr
                    ON agent_events(correlation_id, id);
                CREATE TABLE IF NOT EXISTS shared_context (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    source_agent TEXT,
                    correlation_id TEXT,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS outreach_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_type TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    order_id TEXT,
                    content TEXT NOT NULL,
                    offer TEXT,
                    reason TEXT,
                    correlation_id TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    needs_review_reason TEXT,
                    created_by TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outreach_status
                    ON outreach_drafts(status, id);
                CREATE TABLE IF NOT EXISTS shop_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    shop_name TEXT,
                    tone TEXT,
                    banned_words TEXT,
                    updated_by TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS turn_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT,
                    intent TEXT,
                    emotion TEXT NOT NULL DEFAULT 'neutral',
                    emotion_level INTEGER NOT NULL DEFAULT 0,
                    requires_human INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turn_signals_created
                    ON turn_signals(created_at);
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
            # 兼容旧库：skill_traces 补 variant 列(灰度 A/B 需区分 live/canary)
            stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)").fetchall()}
            if "variant" not in stcols:
                conn.execute("ALTER TABLE skill_traces ADD COLUMN variant TEXT DEFAULT 'live'")
                conn.execute("UPDATE skill_traces SET variant = 'live' WHERE variant IS NULL")
                conn.commit()
            # 兼容旧库：skill_traces 补 skill_version 列(0=未知,历史行无从考证)
            stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)")}
            if "skill_version" not in stcols:
                conn.execute("ALTER TABLE skill_traces ADD COLUMN skill_version INTEGER DEFAULT 0")
                conn.execute("UPDATE skill_traces SET skill_version = 0 WHERE skill_version IS NULL")
                conn.commit()
            # 兼容旧库：skill_traces 补 skill_fingerprint 列("unknown"=未知,历史行
            # 无从考证)。与 skill_version 并存而非取代它:整数版本号只在正式(live)
            # 目录转正/回滚时才递增,候选目录从不带 .version,灰度期读到的版本号
            # 因此永远是"文件不存在→1"——两批不同候选的轨迹无法靠版本号区分。
            # 指纹是被服务那棵树的内容哈希,不依赖任何人在候选创建时打标,天然
            # 覆盖 live/candidate 两种情况。
            stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)")}
            if "skill_fingerprint" not in stcols:
                conn.execute(
                    "ALTER TABLE skill_traces ADD COLUMN skill_fingerprint TEXT DEFAULT 'unknown'")
                conn.execute(
                    "UPDATE skill_traces SET skill_fingerprint = 'unknown' "
                    "WHERE skill_fingerprint IS NULL")
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

    def archive_session_if_changed(self, session_id: str, user_id: str,
                                   messages: list, summary: Optional[str]) -> bool:
        """仅当该会话内容有增长时才归档,返回是否真的写入。

        archive_session 是纯 INSERT,反复调用会造成同一会话多行归档 —— 那会让离线
        聚类过度加权同一段对话。故按 msg_count 比对最近一条归档:相同则跳过。
        保留"内容增长才追加一行"的语义(审计仍能看到演进过程),而不是覆盖。
        """
        count = len(messages or [])
        if count == 0:
            return False
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT msg_count FROM session_archive WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
            if row is not None and (row["msg_count"] or 0) >= count:
                return False
        finally:
            conn.close()
        self.archive_session(session_id, user_id, messages, summary)
        return True

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
                           tool_calls: list[dict], outcome: str,
                           variant: str = "live", skill_version: int = 0,
                           skill_fingerprint: str = "unknown") -> None:
        """记录一轮 skill 执行轨迹。variant 区分现行版/灰度候选,供 A/B 判定。

        skill_version:本轮**加载那一刻**的技能目录版本号,0=未知(老调用方/历史行
        不传即落 0,不假装是第 1 版——同一 skill 转正两次后仍要能按版本分开归因)。

        skill_fingerprint:本轮**加载那一刻**被服务的那棵树的内容指纹,"unknown"=
        未知(老调用方/历史行不传即落 unknown,同样不瞎猜)。与 skill_version 互补:
        version 只在正式目录转正/回滚时递增,候选目录从不带 .version,灰度期永远
        读到"1"——指纹不依赖任何创建候选的地方打标,直接对被服务的树现算内容哈希,
        天然能把两批不同候选的轨迹分开归因(这正是本列存在的理由)。
        """
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
                "outcome, created_at, variant, skill_version, skill_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, skill_name,
                 json.dumps(tool_calls or [], ensure_ascii=False), outcome,
                 self._now(), variant, skill_version, skill_fingerprint or "unknown"),
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

    # ---------- Skill 灰度登记(分级授权:候选按会话接管部分流量) ----------
    def start_canary(self, skill_name: str, candidate_path: str, percent: int,
                     risk: str, policy: str) -> None:
        """登记一个活跃灰度。同 skill 已有活跃记录先置 superseded,保证同时只有一个。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE skill_canaries SET status = 'superseded', finished_at = ? "
                "WHERE skill_name = ? AND status = 'active'", (self._now(), skill_name))
            conn.execute(
                "INSERT INTO skill_canaries (skill_name, candidate_path, percent, risk, "
                "policy, status, started_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (skill_name, candidate_path, percent, risk, policy, self._now()))
            conn.commit()
        finally:
            conn.close()

    def get_active_canary(self, skill_name: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM skill_canaries WHERE skill_name = ? AND status = 'active' "
                "ORDER BY id DESC LIMIT 1", (skill_name,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_active_canaries(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM skill_canaries WHERE status = 'active' "
                "ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def finish_canary(self, skill_name: str, status: str) -> bool:
        """结束该 skill 的活跃灰度(status: promoted / rolled_back)。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE skill_canaries SET status = ?, finished_at = ? "
                "WHERE skill_name = ? AND status = 'active'",
                (status, self._now(), skill_name))
            conn.commit()
            return cur.rowcount > 0
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

    # ---------- 多 Agent 协作总线(持久化 append-only 事件) ----------
    def publish_event(self, event_type: str, payload: dict, source_agent: str,
                      target_agent: str, correlation_id: str) -> int:
        """发布一条协作事件,返回自增 id。

        总线是**持久化**的:进程重启不丢事件,且 correlation_id 把一条协作链
        (信号→洞察→草稿→发送)串起来,全链可回溯审计。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "INSERT INTO agent_events (event_type, payload, source_agent, "
                "target_agent, correlation_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (event_type, json.dumps(payload or {}, ensure_ascii=False),
                 source_agent, target_agent, correlation_id, self._now()))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def claim_events(self, target_agent: str, limit: int = 20) -> list[dict]:
        """**原子认领**该 Agent 的待处理事件:pending → processing,并返回认领到的行。

        幂等的关键:UPDATE 带 `status='pending'` 条件,两个 worker 并发时只有一个
        能把某行改成 processing,另一个的 rowcount 为 0 拿不到它。绝不能改成
        "先 SELECT 再 UPDATE"——那样同一条异常会产出两份洞察/两份草稿。

        坏 JSON 的 payload 行跳过(与 list_skill_traces 同口径),单条脏数据不拖垮
        整个消费循环;但它已被置为 processing,不会反复卡住队列。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM agent_events WHERE target_agent = ? AND status = 'pending' "
                "ORDER BY id ASC LIMIT ?", (target_agent, limit)).fetchall()
            claimed: list[dict] = []
            for row in rows:
                cur = conn.execute(
                    "UPDATE agent_events SET status = 'processing', consumed_at = ? "
                    "WHERE id = ? AND status = 'pending'", (self._now(), row["id"]))
                if cur.rowcount == 0:
                    continue          # 已被别的 worker 认领
                full = conn.execute(
                    "SELECT * FROM agent_events WHERE id = ?", (row["id"],)).fetchone()
                item = dict(full)
                try:
                    item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
                except (json.JSONDecodeError, TypeError):
                    # 脏行已置 processing,不会反复卡队列;但不能悄悄消失——
                    # 这是一次真实的数据损坏,必须留痕供排查。
                    logger.warning(
                        "claim_events: agent_events id=%s target_agent=%s "
                        "correlation_id=%s payload 无法解析为 JSON,已置 processing "
                        "但跳过返回(不会再被 claim_events 捞到,需人工核查)",
                        item.get("id"), item.get("target_agent"),
                        item.get("correlation_id"))
                    continue
                claimed.append(item)
            conn.commit()
            return claimed
        finally:
            conn.close()

    def finish_event(self, event_id: int, status: str) -> bool:
        """结束一条事件(status: done / failed)。只对 processing 的行生效。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE agent_events SET status = ? WHERE id = ? AND status = 'processing'",
                (status, event_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def reclaim_stale_events(self, older_than_seconds: int = 300) -> int:
        """回收滞留在 processing 超过阈值的事件:processing → pending。

        worker 认领(claim_events)后若在 finish_event 之前崩溃,该行会永久卡在
        processing——没有超时/恢复机制的话,重启的 worker 再也捞不到它。本方法
        按 consumed_at 与阈值比较,把过期的 processing 行放回 pending,下一次
        claim_events 即可重新认领。

        条件 UPDATE(status = 'processing' AND consumed_at <= 阈值)与
        claim_events/finish_event 同一套幂等纪律:只改状态仍是 processing 且确实
        过期的行,并发调用互不冲突;done/failed 行的 status 不是 processing,
        永远不会被本方法触碰。返回被回收的行数。
        """
        from datetime import timedelta
        threshold = (datetime.now() - timedelta(seconds=older_than_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE agent_events SET status = 'pending', consumed_at = NULL "
                "WHERE status = 'processing' AND consumed_at IS NOT NULL "
                "AND consumed_at <= ?",
                (threshold,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def list_events(self, correlation_id: Optional[str] = None,
                    limit: int = 100) -> list[dict]:
        """按 id DESC 列事件(可按协作链过滤),供时间线可视化与审计。"""
        sql = "SELECT * FROM agent_events"
        params: list = []
        if correlation_id:
            sql += " WHERE correlation_id = ?"
            params.append(correlation_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
                except (json.JSONDecodeError, TypeError):
                    item["payload"] = {}
                results.append(item)
            return results
        finally:
            conn.close()

    # ---------- 共享上下文池(跨 Agent 可读写,带来源与 TTL) ----------
    def set_shared_context(self, key: str, value: dict, source_agent: str,
                           correlation_id: str, ttl_seconds: int = 86400) -> None:
        """写入共享上下文。必带 source_agent:读到的一方要知道这条是谁写的。"""
        from datetime import datetime, timedelta
        expires = (datetime.now() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO shared_context (key, value, source_agent, correlation_id, "
                "updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "source_agent = excluded.source_agent, "
                "correlation_id = excluded.correlation_id, "
                "updated_at = excluded.updated_at, expires_at = excluded.expires_at",
                (key, json.dumps(value or {}, ensure_ascii=False), source_agent,
                 correlation_id, self._now(), expires))
            conn.commit()
        finally:
            conn.close()

    def get_shared_context(self, key: str) -> Optional[dict]:
        """读共享上下文。已过期或坏 JSON 一律返回 None(读侧不能拿到半截数据)。"""
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM shared_context WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            if item.get("expires_at") and item["expires_at"] <= self._now():
                return None
            try:
                item["value"] = json.loads(item["value"]) if item["value"] else {}
            except (json.JSONDecodeError, TypeError):
                return None
            return item
        finally:
            conn.close()

    def list_shared_context(self, prefix: str = "", limit: int = 50,
                            correlation_id: Optional[str] = None) -> list[dict]:
        """按 key 前缀列未过期的共享上下文(供控制台展示"当前共享了什么")。

        `correlation_id` 非空时**在 SQL 里**过滤到那一条协作链。这不是可有可无的
        优化:LIMIT 作用在 `ORDER BY updated_at DESC` 之上,而 shared_context 每
        条被升级的会话、每个异常商品都会写一行,很快就超过 limit;若先取最近
        limit 行再在 Python 里筛链路,稍旧一点的协作链就会拿到空的 shared 列表
        ——行还在库里,时间线却说"这条链没共享过任何上下文"。
        """
        conn = self.connect()
        sql = ("SELECT * FROM shared_context WHERE key LIKE ? AND "
               "(expires_at IS NULL OR expires_at > ?)")
        params: list = [f"{prefix}%", self._now()]
        if correlation_id:
            sql += " AND correlation_id = ?"
            params.append(correlation_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["value"] = json.loads(item["value"]) if item["value"] else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                results.append(item)
            return results
        finally:
            conn.close()

    # ---------- 触达草稿(营销 Agent 只产草稿,发送必须人工批准) ----------
    def create_outreach_draft(self, opportunity_type: str, user_id: str,
                              order_id: str, content: str, offer: dict,
                              reason: str, correlation_id: str, created_by: str,
                              needs_review_reason: str = "") -> int:
        """落一条触达草稿(status 恒为 draft)。

        **本方法是营销 Agent 唯一的写路径**:它永远只能产 draft,发送发生在
        审批端点里。needs_review_reason 非空表示命中了承诺类敏感词,人工要重点看。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "INSERT INTO outreach_drafts (opportunity_type, user_id, order_id, "
                "content, offer, reason, correlation_id, status, needs_review_reason, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                (opportunity_type, user_id, order_id, content,
                 json.dumps(offer or {}, ensure_ascii=False), reason, correlation_id,
                 needs_review_reason, created_by, self._now()))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _draft_from_row(self, row) -> Optional[dict]:
        item = dict(row)
        try:
            item["offer"] = json.loads(item["offer"]) if item["offer"] else {}
        except (json.JSONDecodeError, TypeError):
            return None
        return item

    def list_outreach_drafts(self, status: Optional[str] = None,
                             limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM outreach_drafts"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [d for d in (self._draft_from_row(r) for r in rows) if d]
        finally:
            conn.close()

    def pending_outreach_targets(self) -> set:
        """当前还挂在待审队列里的触达对象集合 `{(user_id, order_id)}`。

        给起草侧做去重用:同一个买家、同一个订单已经躺着一条等人审的草稿时,
        再排一条近乎重复的进去没有任何新增信息,只会稀释审批队列——而店主的
        注意力正是这条链上最稀缺的资源,他挨个批下去就等于给同一个人连发几条。

        只看 `status='draft'`(等人看的那些)。已 approved/sent 的不在其中:那是
        "已经处理过的历史",不该永久封杀对同一个订单的再次触达;rejected 同理,
        店主明确驳回过的内容,下一轮换个说法重新排队是合理的。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT user_id, order_id FROM outreach_drafts WHERE status = 'draft'"
            ).fetchall()
            return {((r["user_id"] or ""), (r["order_id"] or "")) for r in rows}
        finally:
            conn.close()

    def get_outreach_draft(self, draft_id: int) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM outreach_drafts WHERE id = ?",
                               (draft_id,)).fetchone()
            return self._draft_from_row(row) if row else None
        finally:
            conn.close()

    def review_outreach_draft(self, draft_id: int, status: str,
                              reviewed_by: str) -> bool:
        """审批草稿(status: approved / rejected)。**只对 draft 状态生效**。

        条件更新是防重发的关键:两次点"批准"只有第一次拿到 True,发送端点据此
        判断本次是否真的该发,不会给同一个买家发两遍。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET status = ?, reviewed_by = ?, reviewed_at = ? "
                "WHERE id = ? AND status = 'draft'",
                (status, reviewed_by, self._now(), draft_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_outreach_sent(self, draft_id: int) -> bool:
        """标记已发送。只对 approved 生效,保证"批准过"才可能"已发送"。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET status = 'sent', sent_at = ? "
                "WHERE id = ? AND status = 'approved'", (self._now(), draft_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def revert_outreach_to_pending(self, draft_id: int) -> bool:
        """投递失败时把草稿从 approved 退回 draft,清空审批痕迹,可重新批准。

        **只对 approved 状态生效**(与 review_outreach_draft / mark_outreach_sent
        同款条件更新):调用方在拿到这次投递失败之前,刚把这条草稿从 draft
        条件更新为 approved 并认领了"本次处理权",所以这里预期的前置状态必是
        approved——用条件更新而不是无条件 UPDATE,是为了让"退回没有真的生效"
        这件事本身可判定(rowcount==0),不装作退回一定成功。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET status = 'draft', reviewed_by = NULL, "
                "reviewed_at = NULL WHERE id = ? AND status = 'approved'",
                (draft_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ---------- 店铺人格(单店:CHECK (id = 1) 把"单行"写进 schema) ----------
    def get_shop_profile(self) -> dict:
        """读店铺人格。无行(从未设置过)返回空 dict——由调用方(load_profile)
        决定空值时的默认语气,这里不掺入任何默认值。"""
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM shop_profile WHERE id = 1").fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def set_shop_profile(self, fields: dict, updated_by: str) -> None:
        """写店铺人格(单行 upsert,id 恒为 1)。只覆盖调用方传入的字段,
        未传的字段保留旧值(用 COALESCE 落到已有行,首次写入落到默认空值)。"""
        conn = self.connect()
        try:
            existing = conn.execute("SELECT * FROM shop_profile WHERE id = 1").fetchone()
            base = dict(existing) if existing else {}
            shop_name = fields.get("shop_name", base.get("shop_name", ""))
            tone = fields.get("tone", base.get("tone", ""))
            banned_words = fields.get("banned_words", base.get("banned_words", ""))
            conn.execute(
                "INSERT INTO shop_profile (id, shop_name, tone, banned_words, "
                "updated_by, updated_at) VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET shop_name = excluded.shop_name, "
                "tone = excluded.tone, banned_words = excluded.banned_words, "
                "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (shop_name, tone, banned_words, updated_by, self._now()))
            conn.commit()
        finally:
            conn.close()

    # ---------- 情绪信号(N2:按每一轮落库,与 skill_traces 分表——skill_traces
    # 只在本轮加载过 skill 时才有行,而情绪要按每一轮统计,塞进去会让分母失真) ----------
    def record_turn_signal(self, session_id: str, user_id: str, intent: str,
                           emotion: str, emotion_level: int,
                           requires_human: bool) -> None:
        """记录一轮的意图/情绪信号。旁路埋点:调用方(chat.py)负责 fail-soft,
        本方法本身只管写入,不做兜底判断。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO turn_signals (session_id, user_id, intent, emotion, "
                "emotion_level, requires_human, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, intent, emotion, int(emotion_level),
                 1 if requires_human else 0, self._now()),
            )
            conn.commit()
        finally:
            conn.close()

    def list_turn_signals(self, limit: int = 200) -> list[dict]:
        """按 id DESC 取最近若干轮信号(测试/排查用)。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM turn_signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def emotion_distribution(self, window_days: int = 7) -> dict:
        """窗口内情绪分布:三档计数 + 激烈(angry)占比。

        除零返回 0.0 而非 None——与 shop_analytics._rate 同口径,消费方是 LLM,
        null 会诱导模型现编数字。
        """
        conn = self.connect()
        try:
            w = f"datetime('now', '-{max(1, int(window_days))} days')"
            rows = conn.execute(
                f"SELECT emotion, COUNT(*) AS n FROM turn_signals "
                f"WHERE created_at >= {w} GROUP BY emotion"
            ).fetchall()
            counts = {"neutral": 0, "unhappy": 0, "angry": 0}
            for r in rows:
                if r["emotion"] in counts:
                    counts[r["emotion"]] = int(r["n"])
            total = sum(counts.values())
            angry_rate = (counts["angry"] / total) if total else 0.0
            return {"window_days": int(window_days), "total": total,
                    "counts": counts, "angry_rate": angry_rate}
        finally:
            conn.close()
