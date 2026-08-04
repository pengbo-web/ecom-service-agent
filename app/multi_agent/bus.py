"""协作总线(Agent Communication Bus):事件驱动的跨 Agent 通信门面。

设计取舍:
- **持久化**而非内存队列。进程重启不丢事件;一条协作链(信号→洞察→草稿→发送)
  按 correlation_id 可完整回溯,这是"企业级可审计"与"demo 级内存队列"的分界。
- **拉取式**而非同步调用。买家会话只负责"发信号"(旁路,fail-soft),分析与营销
  在 worker 里异步跑,买家那一轮的延迟零增加。
- **消费幂等**由数据层的条件更新保证(见 Database.claim_events)。
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable, Optional

from app.db import get_db

logger = logging.getLogger(__name__)

# Agent 标识(同时是 target_agent 的取值域)
AGENT_SERVICE = "service"     # 客服服务 Agent(C 端)
AGENT_ANALYST = "analyst"     # 店铺参谋 Agent(B 端只读)
AGENT_GROWTH = "growth"       # 营销增长 Agent(B 端草稿型)
AGENT_HUMAN = "human"         # 人工闸:需要人来看的事件投给它

# 事件类型
EV_SIGNAL_ANOMALY = "signal.anomaly"        # 客服侧/扫描器发现异常
EV_INSIGHT_DIAGNOSIS = "insight.diagnosis"  # 参谋出诊断结论
EV_DRAFTS_READY = "action.drafts_ready"     # 营销出好草稿,待人工审批
EV_OUTREACH_SENT = "result.outreach_sent"   # 人工批准并发出,闭环回写


def new_correlation_id(prefix: str = "C") -> str:
    """一条协作链的 id。用 uuid4 而非时间戳:同一秒可能起多条链。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def publish(event_type: str, payload: dict, source: str, target: str,
            correlation_id: Optional[str] = None) -> Optional[str]:
    """发布事件,返回这条协作链的 correlation_id;失败返回 None。

    **fail-soft**:发布点之一在买家会话的热路径上(客服 Agent 轮末埋点),
    总线不可用绝不能让买家那一轮失败,所以异常一律吞掉记日志——与既有
    skill_trace 埋点同一姿态。
    """
    from app.config.settings import settings

    if not getattr(settings, "collab_enabled", True):
        return None
    corr = correlation_id or new_correlation_id()
    try:
        get_db().publish_event(event_type, payload or {}, source, target, corr)
        return corr
    except Exception as exc:  # noqa: BLE001 旁路埋点,绝不影响主链路
        logger.warning("协作事件发布失败(已忽略): %s %s", event_type, exc)
        return None


def consume(target: str, handler: Callable[[dict], None], limit: int = 20) -> dict:
    """认领并处理该 Agent 的待处理事件,返回 {claimed, done, failed}。

    单个处理器抛异常只把**那一条**置 failed,不影响同批其它事件——一个坏事件
    不能卡死整条流水线。failed 的事件不会被自动重试(避免坏事件无限循环),
    留在表里供人工在时间线上看到并决定。
    """
    from app.config.settings import settings

    stats = {"claimed": 0, "done": 0, "failed": 0}
    if not getattr(settings, "collab_enabled", True):
        return stats
    try:
        db = get_db()
        events = db.claim_events(target, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("协作事件认领失败: %s", exc)
        return stats

    stats["claimed"] = len(events)
    for ev in events:
        try:
            handler(ev)
        except Exception as exc:  # noqa: BLE001 单条失败不拖垮整批
            logger.exception("协作事件处理失败 id=%s type=%s: %s",
                             ev.get("id"), ev.get("event_type"), exc)
            db.finish_event(int(ev["id"]), "failed")
            stats["failed"] += 1
            continue
        db.finish_event(int(ev["id"]), "done")
        stats["done"] += 1
    return stats
