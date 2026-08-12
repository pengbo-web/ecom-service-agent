"""坐席队列（SQLite 持久化）。"""

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from app.config.settings import settings


class HandoffQueue:
    def __init__(self, db_path: Optional[str] = None, id_factory=None):
        self.db_path = db_path or settings.hitl_db_path
        self._id = id_factory or (lambda: uuid.uuid4().hex[:12])

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
                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    intent TEXT,
                    reasons TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_handoff_status ON handoffs(status);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def add(self, bundle: dict) -> str:
        hid = self._id()
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO handoffs (handoff_id, session_id, intent, reasons, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (hid, bundle.get("session_id"), bundle.get("intent"),
                 json.dumps(bundle.get("reasons", []), ensure_ascii=False),
                 json.dumps(bundle, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        return hid

    def _row(self, r) -> dict:
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"]) if d.get("reasons") else []
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
        return d

    def list_pending(self) -> list:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM handoffs WHERE status='pending' ORDER BY created_at DESC"
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    def list_pending_sessions(self) -> list:
        """坐席工作队列:**一个买家在等 = 一行**,而不是一次升级判定 = 一行。

        **实测缺陷。** `list_pending()` 返回的是原始升级记录,坐席界面直接渲染它,
        于是队列里 50 条待办实际只来自 **7 个会话**——其中一个会话占了 **37 条**
        (跨度 08-02 到 08-12,intent 里 23 条是 return_request)。后果:

        - 队列深度失真 7 倍。坐席看到 50,以为有 50 个人在等;
        - `resolve` 是按 handoff_id 逐条的,**要点 37 次才能清掉一个买家**;
        - 处理完一条,同一个买家立刻又冒出来——看起来永远处理不完。

        这与"324 条陈旧人工待办""30 条相同的假警报"是同一类:**计数单位错了 →
        队列失去意义 → 告警疲劳**。而 `SeatView` 的标题写的本来就是「待接管会话」,
        界面自己说的单位就是会话。

        **升级记录逐条保留不动**(那是审计轨迹,每次升级确实发生过),这里只改
        "坐席从哪个视图工作"。每行给出:

          waiting_since  最早那次升级的时间——**排队优先级看这个**,不是最近一次
          latest         最近一次升级(现在在问什么)
          escalations    这个会话累计升级了多少次(反复升级本身就是信号)
          handoff_ids    全部待处理的 id,供 `resolve_session` 一次清干净
        """
        rows = self.list_pending()
        by_session: dict = {}
        for r in rows:   # list_pending 已按 created_at DESC 排序
            sid = r.get("session_id") or ""
            g = by_session.get(sid)
            if g is None:
                # 第一条见到的就是最新的一条(DESC),它代表"现在在问什么"
                by_session[sid] = {
                    "session_id": sid,
                    "latest": r,
                    "waiting_since": r.get("created_at"),
                    "escalations": 1,
                    "handoff_ids": [r.get("handoff_id")],
                }
                continue
            g["escalations"] += 1
            g["handoff_ids"].append(r.get("handoff_id"))
            # DESC 序下后见到的更早,waiting_since 一路往前推
            if (r.get("created_at") or "") < (g["waiting_since"] or ""):
                g["waiting_since"] = r.get("created_at")
        # 等最久的排前面:坐席该先处理等待时间最长的那个买家
        return sorted(by_session.values(), key=lambda g: g["waiting_since"] or "")

    def resolve_session(self, session_id: str) -> int:
        """把一个会话的**全部**待处理升级标记为已解决,返回实际影响的条数。

        坐席处理的是"这个买家",不是"这一次升级判定"——逐条 resolve 会让一个买家
        需要点 37 次(实测),而中间任何一次遗漏都会让这个会话重新出现在队列里。
        条件更新只对 pending 生效,与 `resolve` 同一套幂等纪律。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE handoffs SET status='resolved', "
                "resolved_at=CURRENT_TIMESTAMP "
                "WHERE session_id=? AND status='pending'",
                (session_id,),
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    def count_pending_sessions(self) -> int:
        """在等的**买家数**。`count_pending()` 数的是升级次数,两者差 7 倍(实测),
        给坐席看的必须是这个。"""
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS c FROM handoffs "
                "WHERE status='pending'"
            ).fetchone()["c"]
        finally:
            conn.close()

    def get(self, handoff_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            r = conn.execute(
                "SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
            return self._row(r) if r else None
        finally:
            conn.close()

    def resolve(self, handoff_id: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE handoffs SET status='resolved', "
                "resolved_at=CURRENT_TIMESTAMP WHERE handoff_id=? AND status='pending'",
                (handoff_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def count_pending(self) -> int:
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM handoffs WHERE status='pending'"
            ).fetchone()["c"]
        finally:
            conn.close()
