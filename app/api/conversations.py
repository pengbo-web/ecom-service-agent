"""会话生命周期服务层(生产语义:服务端签发,一段对话一个会话)。

铁律:conversation_id 只由服务端生成;客户端传来的未知 ID(旧格式/伪造)
一律不采纳,由 ensure_active 换发新会话——防会话伪造,也兼容旧数据平滑过渡。
"""

from __future__ import annotations


def open_or_reuse(db, user_id: str) -> dict:
    """取该用户最近的 open 会话(多端/刷新一致);没有则服务端新开一个。"""
    existing = db.latest_open_conversation(user_id)
    if existing is not None:
        return existing
    return db.create_conversation(user_id)


def ensure_active(db, session_id: str, user_id: str) -> tuple[str, bool]:
    """确保拿到一个可用(open)且**属于该用户**的会话 ID;返回 (生效ID, 是否翻篇/换发)。

    归属校验:拿到别人的 open 会话 ID 也不能写入——按"未知 ID"处理直接换发
    (而非 403,不泄露该 ID 是否存在/归属谁)。
    """
    conv = db.get_conversation(session_id)
    if conv is not None and conv["status"] == "open" and conv.get("user_id") == user_id:
        return session_id, False
    return db.create_conversation(user_id)["conversation_id"], True
