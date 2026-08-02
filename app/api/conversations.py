"""会话生命周期服务层(生产语义:服务端签发,一段对话一个会话)。

铁律:conversation_id 只由服务端生成;客户端传来的未知 ID(旧格式/伪造)
一律不采纳,由 ensure_active 换发新会话——防会话伪造,也兼容旧数据平滑过渡。
"""

from __future__ import annotations


def open_or_reuse(db, user_id: str) -> dict:
    """单一连续会话:一个用户永远复用同一条"规范会话"(最近活跃的那条,关了就重开),
    只有该用户从无会话时才新建。→ 登录必显示其全部历史,绝不碎片化。"""
    existing = db.latest_conversation(user_id)
    if existing is not None:
        if existing.get("status") != "open":
            db.reopen_conversation(existing["conversation_id"])
            existing["status"] = "open"
        return existing
    return db.create_conversation(user_id)


def ensure_active(db, session_id: str, user_id: str) -> tuple[str, bool]:
    """确保拿到一个可用(open)且**属于该用户**的会话 ID;返回 (生效ID, 是否切换)。

    单一连续会话:客户端传来的 ID 若非"本人的 open 会话"(未知/旧格式/伪造/别人的/已关),
    一律回落到该用户的规范会话(复用/重开),而**不再新建碎片**。
    归属校验保留:别人的会话 ID 不被采纳(零信息泄露)。
    """
    conv = db.get_conversation(session_id)
    if conv is not None and conv["status"] == "open" and conv.get("user_id") == user_id:
        return session_id, False
    canonical = open_or_reuse(db, user_id)
    cid = canonical["conversation_id"]
    return cid, cid != session_id
