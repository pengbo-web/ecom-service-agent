"""灰度路由:让候选 skill 按会话哈希接管一部分真实流量,与现行版本 A/B。

为什么按 session 哈希而不是随机数:同一通对话必须始终看到同一个版本,否则顾客
会在一次会话里被两套流程处理。哈希是确定性的,进程重启/多实例部署结果一致。
"""

from __future__ import annotations

import hashlib

VARIANT_LIVE = "live"
VARIANT_CANARY = "canary"


def in_canary_bucket(skill_name: str, session_id: str, percent: int) -> bool:
    """该会话是否落进这个 skill 的灰度桶。percent<=0 恒 False,>=100 恒 True。

    哈希里带 skill_name:同一会话在不同 skill 上的分桶相互独立,避免"某个会话
    永远是灰度组"这种系统性偏斜。
    """
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.md5(f"{skill_name}:{session_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < percent
