"""会话冷归档:会话结束/被回收时,把完整会话落到持久库(SQLite),永久留存。

热会话在 Redis(带 TTL 会过期),归档解决"过期后还要审计/离线分析/数据飞轮"的需求。
默认 NullSessionArchiver(不归档);生产路径注入 SqliteSessionArchiver。best-effort,不影响主流程。
"""

from __future__ import annotations


class NullSessionArchiver:
    """不归档(默认;测试与单机可选关闭)。"""
    def archive(self, session_id: str, agent) -> None:  # noqa: D401
        return None


class SqliteSessionArchiver:
    """把会话完整落到 SQLite session_archive 表。"""
    def archive(self, session_id: str, agent) -> None:
        try:
            from app.db import get_db
            get_db().archive_session(
                session_id=session_id,
                user_id=getattr(agent, "user_id", "default"),
                messages=list(getattr(agent, "raw_messages", []) or []),
                summary=getattr(agent, "summary", None),
            )
        except Exception:  # noqa: BLE001 归档 best-effort,失败不影响会话回收
            pass


def build_archiver(enabled: bool):
    return SqliteSessionArchiver() if enabled else NullSessionArchiver()
