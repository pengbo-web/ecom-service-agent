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
    """把会话完整落到 SQLite session_archive 表。

    去重:通过 archive_session_if_changed 写入,内容没有增长(msg_count 未变)
    时跳过,避免同一会话被反复归档成多行,过度加权到离线聚类里。
    """
    def archive(self, session_id: str, agent) -> None:
        try:
            from app.db import get_db
            db = get_db()
            messages = list(getattr(agent, "raw_messages", []) or [])
            summary = getattr(agent, "summary", None)
            db.archive_session_if_changed(
                session_id=session_id,
                user_id=getattr(agent, "user_id", "default"),
                messages=messages,
                summary=summary,
            )
            # WS2:旁路增量索引进归档 FTS(门禁用例第三采样源)。fail-soft:
            # 索引坏了只影响采样面,归档与合成主流程照跑(archive_fts 内部自吞)。
            from app.agent.skills import archive_fts
            archive_fts.index_session(db, session_id, messages, summary)
        except Exception:  # noqa: BLE001 归档 best-effort,失败不影响会话回收
            pass


def build_archiver(enabled: bool):
    return SqliteSessionArchiver() if enabled else NullSessionArchiver()
