"""结构化用户档案(H2.3/G2):base(会员等级/联系方式等) + tags(行为标签) +
tickets(工单流转)。

设计目标与 fts_store.py 一致:手写 sqlite3、WAL、单独的存储组件,不侵入
short_term/long_term。向后兼容:无档案时 `UserProfile.to_prompt()` 返回
None,注入侧零影响。门控 `settings.memory_profile_enabled` 默认开、关闭
即完全回退(不建库、不注入、不落工单)。

best-effort 铁律:`record_ticket` 与 `sync_base_from_db` 内部任何异常均
静默吞掉,绝不影响对话主流程(HITL 升级、业务库同步都是"锦上添花")。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.config.settings import settings


@dataclass
class UserProfile:
    """一个用户的结构化档案(内存态,供 to_prompt 注入)。"""

    user_id: str
    base: dict
    tags: list[str] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)

    def to_prompt(self) -> str | None:
        """生成注入 system prompt 的档案片段;base/tags/tickets 三者全空返回 None。"""
        if not self.base and not self.tags and not self.tickets:
            return None

        # 档案里的字段(基础信息、工单原因)同样来自买家发言与历史会话,以 role=system
        # 注入而不过输入护栏。框定身份,见 app/agent/data_framing.py。
        from app.agent.data_framing import frame

        lines = [f"该用户的结构化档案{frame('字段来自该买家的历史会话与工单记录')}："]
        if self.base:
            base_text = ", ".join(f"{k}={v}" for k, v in self.base.items())
            lines.append(f"- 基础信息：{base_text}")
        if self.tags:
            lines.append(f"- 行为标签：{'、'.join(self.tags)}")
        if self.tickets:
            recent = self.tickets[-3:]
            tickets_text = "，".join(
                f"[{t.get('status')}] {t.get('reason')} ({t.get('ts')})" for t in recent
            )
            lines.append(f"- 最近工单：{tickets_text}")
        return "\n".join(lines)


class UserProfileStore:
    """结构化用户档案的 SQLite 持久化(WAL,建表参考 fts_store.py 风格)。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        # 进程级单例会被 FastAPI 线程池的不同 worker 线程访问:
        # check_same_thread=False 允许跨线程,配合 self._lock 串行化所有读写。
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS user_profile ("
            "user_id TEXT PRIMARY KEY, base_json TEXT, tags_json TEXT, updated_at TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS user_tickets ("
            "user_id TEXT, ticket_id TEXT, status TEXT, reason TEXT, ts TEXT)"
        )
        self._conn.commit()

    def get(self, user_id: str) -> UserProfile:
        with self._lock:
            row = self._conn.execute(
                "SELECT base_json, tags_json FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            ticket_rows = self._conn.execute(
                "SELECT ticket_id, status, reason, ts FROM user_tickets "
                "WHERE user_id = ? ORDER BY ts ASC",
                (user_id,),
            ).fetchall()
        base = json.loads(row[0]) if row and row[0] else {}
        tags = json.loads(row[1]) if row and row[1] else []
        tickets = [
            {"ticket_id": r[0], "status": r[1], "reason": r[2], "ts": r[3]}
            for r in ticket_rows
        ]
        return UserProfile(user_id=user_id, base=base, tags=tags, tickets=tickets)

    def update_base(self, user_id: str, patch: dict) -> None:
        """合并式更新:读旧 base、dict.update(patch)、写回。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT base_json, tags_json FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            base = json.loads(row[0]) if row and row[0] else {}
            tags_json = row[1] if row and row[1] else json.dumps([])
            base.update(patch)
            now = datetime.now().isoformat()
            self._conn.execute(
                "INSERT INTO user_profile (user_id, base_json, tags_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET base_json = ?, updated_at = ?",
                (user_id, json.dumps(base, ensure_ascii=False), tags_json, now,
                 json.dumps(base, ensure_ascii=False), now),
            )
            self._conn.commit()

    def add_tag(self, user_id: str, tag: str) -> None:
        """去重追加行为标签。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT base_json, tags_json FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            base_json = row[0] if row and row[0] else json.dumps({})
            tags = json.loads(row[1]) if row and row[1] else []
            if tag not in tags:
                tags.append(tag)
            now = datetime.now().isoformat()
            self._conn.execute(
                "INSERT INTO user_profile (user_id, base_json, tags_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET tags_json = ?, updated_at = ?",
                (user_id, base_json, json.dumps(tags, ensure_ascii=False), now,
                 json.dumps(tags, ensure_ascii=False), now),
            )
            self._conn.commit()

    def add_ticket(self, user_id: str, ticket_id: str, status: str, reason: str) -> None:
        ts = datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_tickets (user_id, ticket_id, status, reason, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, ticket_id, status, reason, ts),
            )
            self._conn.commit()

    def sync_base_from_db(self, user_id: str) -> None:
        """best-effort 从业务库 users 表同步 name;查不到/异常静默跳过。"""
        try:
            from app.db import get_db

            conn = get_db().connect()
            try:
                row = conn.execute(
                    "SELECT name FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
            finally:
                conn.close()
            if row and row["name"]:
                self.update_base(user_id, {"name": row["name"]})
        except Exception:
            pass

    def close(self) -> None:
        self._conn.close()


_STORE: UserProfileStore | None = None


def get_profile_store() -> UserProfileStore | None:
    """单例;门控关闭返回 None;db 路径 = memory_dir/profile.db。"""
    global _STORE
    if not settings.memory_profile_enabled:
        return None
    if _STORE is None:
        db_path = Path(settings.memory_dir) / "profile.db"
        _STORE = UserProfileStore(str(db_path))
    return _STORE


def set_profile_store(store: UserProfileStore | None) -> None:
    """测试注入/复位单例;传 None 复位后下次 get_profile_store 重建。"""
    global _STORE
    _STORE = store


def record_ticket(user_id: str | None, ticket_id: str, status: str, reason: str) -> None:
    """best-effort 落一条工单流转记录;user_id 为空/门控关闭/异常均静默跳过。"""
    if not user_id:
        return
    try:
        store = get_profile_store()
        if store is None:
            return
        store.add_ticket(user_id, ticket_id, status, reason)
    except Exception:
        pass
