"""共享记忆池(Shared Memory):三个 Agent 都可读写的跨会话上下文。

与"会话记忆"的区别:会话记忆按 user_id 隔离、服务单个买家;共享上下文是
**店铺级**的,参谋写的诊断结论要能被营销读到。所以每条必带 source_agent 与
correlation_id——读的一方要知道这是谁在哪条协作链上写的,不能当成客观事实。

安全:共享内容里可能含用户可控文本(买家咨询原文进了诊断摘要),注入 prompt
时一律走 render_context_block 加数据围栏,与 app/agent/product_context.py 同手法。

**存储后端可插拔**(settings.shared_context_backend):
- ``sqlite``(默认):走 Database 类,与事件总线/业务数据共用同一个 SQLite 文件。
- ``redis``:复用项目现有 Redis(session_store 同一个实例),原生 TTL 自动清理、
  多实例共享无文件锁竞争。挂掉时 fail-soft,**不回退 SQLite**——shared_context
  是可重算的(Worker 下一轮扫描会重新生成诊断),丢一轮只影响本次注入,不会损
  害数据正确性。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from app.db import get_db

logger = logging.getLogger(__name__)

KEY_DIAGNOSIS = "diagnosis"       # 参谋对某商品/某 skill 的归因结论
KEY_ANOMALY = "anomaly"           # 扫描器发现的异常快照
KEY_OPPORTUNITY = "opportunity"   # 营销识别出的商机摘要


def make_key(kind: str, subject: str) -> str:
    """键规范 `kind:subject`,例如 `diagnosis:P001`。前缀便于按类列举。"""
    return f"{kind}:{subject}"


# ---- Redis 后端基础设施(惰性单例 + Breaker 冷却窗口)----
#
# 与 hmdp_identity.py 同模式:模块级 client + Breaker 各一个,首次调用时构造。
# 不复用 session/store.py 的 RedisSessionStore——那个类是面向会话 JSON 整块
# 存取的,这里的 key 结构和索引需求(Sorted Set 按时间排序)都不同,硬套只会
# 让两边各自变形。直接拿 make_client 构造原生 redis-py client。

_redis_client = None
_breaker = None


def _get_redis():
    """惰性获取 Redis 客户端 + Breaker(首次调用时构造,后续复用)。"""
    global _redis_client, _breaker
    if _redis_client is None:
        from app.session.redis_health import Breaker, make_client
        from app.config.settings import settings

        _redis_client = make_client(settings.redis_url)
        _breaker = Breaker(
            float(getattr(settings, "session_store_redis_retry_cooldown_s", 5.0)),
            name="shared_context",
        )
    return _redis_client, _breaker


def _use_redis() -> bool:
    """是否走 Redis 后端。"""
    from app.config.settings import settings

    return getattr(settings, "shared_context_backend", "sqlite") == "redis"


#: 索引 Sorted Set 的上一个 score。见 `_next_score`。
_last_score: float = 0.0


def _next_score(now_ts: float) -> float:
    """给索引 ZADD 取一个**严格递增**的 score。

    **为什么不能直接用时间戳。** Windows 上 `datetime.now()` 的实际精度约 10ms
    ——实测连续 6 次调用只得到 2 个不同值。同一 tick 内写入的两条拿到相同 score,
    而 `ZREVRANGE` 对同分成员退化成按 member **字典序倒序**,与写入顺序无关:
    先写 OLD 后写 NEW,取回来第一条是 OLD(字典序 OLD > NEW)。
    也就是说"最近写入的排在前面"这个保证在同一毫秒内**根本不成立**,
    而一条协作链上的多条诊断恰恰常常是连着写的。

    做法:score 取 `max(当前时间, 上一个 score + 1μs)`。同 tick 内连写时靠那
    1μs 递增拉开顺序,时钟走动后立刻回到真实时间,不会持续漂移。

    **已知边界(不掩饰)**:计数器是**进程内**的,多实例并发写同一毫秒时仍可能
    错序。真要跨进程严格有序得让 Redis 发号(多一次往返),而同一毫秒内跨实例的
    "先后"本身就没有可靠定义 —— 不值得为它加一次 RTT。
    """
    global _last_score
    _last_score = max(now_ts, _last_score + 1e-6)
    return _last_score


def _reset_redis() -> None:
    """重置模块级 Redis 单例(测试用:注入 fakeredis client 后需要清除旧实例)。"""
    global _redis_client, _breaker
    _redis_client = None
    _breaker = None


# ---- Redis 连接异常(惰性 import,只在首次使用时拿一次)----
#
# 与 store.py 的 RedisSessionStore.__init__ 同样从 redis.exceptions 拿,不写死
# 具体异常类名——redis-py 版本升级可能调整继承树,跟着包走比手动列举更安全。
_redis_conn_errors: Optional[tuple] = None


def _get_conn_errors() -> tuple:
    global _redis_conn_errors
    if _redis_conn_errors is None:
        import redis.exceptions as _re

        _redis_conn_errors = (_re.ConnectionError, _re.TimeoutError)
    return _redis_conn_errors


def _decode(val):
    """Redis 返回的 bytes → str;已经是 str 的原样返回。"""
    return val.decode("utf-8") if isinstance(val, bytes) else val


# ---- Redis 后端实现 ----


def _redis_share(
    kind: str,
    subject: str,
    value: dict,
    source_agent: str,
    correlation_id: str,
    ttl_seconds: int,
) -> bool:
    """Redis 写入:Hash(条目) + EXPIRE(TTL) + Sorted Set(索引),pipeline 原子提交。"""
    client, breaker = _get_redis()
    if breaker.open:
        return False

    key = f"shared:ctx:{kind}:{subject}"
    idx_key = f"shared:idx:{kind}"
    now = datetime.now()
    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        pipe = client.pipeline()
        pipe.hset(
            key,
            mapping={
                "value": json.dumps(value or {}, ensure_ascii=False),
                "source_agent": source_agent,
                "correlation_id": correlation_id,
                "updated_at": now_iso,
            },
        )
        pipe.expire(key, ttl_seconds)
        # 索引:score = Unix timestamp,member = subject;用于 recent_entries 按时间倒序取
        pipe.zadd(idx_key, {subject: _next_score(now.timestamp())})
        pipe.execute()
        breaker.record_success()
        return True
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, _get_conn_errors()):
            breaker.record_failure("share", exc)
        else:
            logger.warning(
                "共享上下文 Redis 写入失败(已忽略): %s:%s %s", kind, subject, exc
            )
        return False


def _redis_fetch_entry(kind: str, subject: str) -> Optional[dict]:
    """Redis 单条读取:HGETALL → 解码 → JSON 解析 value。"""
    client, breaker = _get_redis()
    if breaker.open:
        return None

    key = f"shared:ctx:{kind}:{subject}"
    try:
        data = client.hgetall(key)
        if not data:
            return None
        breaker.record_success()
        item = {_decode(k): _decode(v) for k, v in data.items()}
        item["key"] = make_key(kind, subject)
        try:
            item["value"] = json.loads(item["value"]) if item.get("value") else {}
        except (json.JSONDecodeError, TypeError):
            return None
        return item
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, _get_conn_errors()):
            breaker.record_failure("fetch", exc)
        else:
            logger.warning(
                "共享上下文 Redis 读取失败(已忽略): %s:%s %s", kind, subject, exc
            )
        return None


def _redis_recent_entries(kind: str, limit: int) -> list[dict]:
    """Redis 按 kind 取最近若干条:ZREVRANGE 索引 → 逐条 HGETALL。"""
    client, breaker = _get_redis()
    if breaker.open:
        return []

    idx_key = f"shared:idx:{kind}"
    try:
        subjects = client.zrevrange(idx_key, 0, limit - 1)
        results = []
        for subj in subjects:
            subj_str = _decode(subj)
            entry = _redis_fetch_entry(kind, subj_str)
            if entry is not None:
                results.append(entry)
        return results
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, _get_conn_errors()):
            breaker.record_failure("recent_entries", exc)
        else:
            logger.warning(
                "共享上下文 Redis 列举失败(已忽略): %s %s", kind, exc
            )
        return []


# ---- 公共 API(后端无关的接口,消费方只看这些)----


def share(
    kind: str,
    subject: str,
    value: dict,
    source_agent: str,
    correlation_id: str,
    ttl_seconds: int = 86400,
) -> bool:
    """写入共享上下文;失败返回 False(fail-soft,不打断调用方的主流程)。"""
    from app.config.settings import settings

    if not getattr(settings, "collab_enabled", True):
        return False
    if _use_redis():
        return _redis_share(
            kind, subject, value, source_agent, correlation_id, ttl_seconds
        )
    try:
        get_db().set_shared_context(
            make_key(kind, subject),
            value or {},
            source_agent,
            correlation_id,
            ttl_seconds,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("共享上下文写入失败(已忽略): %s:%s %s", kind, subject, exc)
        return False


def fetch(kind: str, subject: str) -> Optional[dict]:
    """只取 value。过期/不存在/坏数据一律 None。"""
    entry = fetch_entry(kind, subject)
    return entry["value"] if entry else None


def fetch_entry(kind: str, subject: str) -> Optional[dict]:
    """取整条(含 source_agent / correlation_id / updated_at)。"""
    if _use_redis():
        return _redis_fetch_entry(kind, subject)
    try:
        return get_db().get_shared_context(make_key(kind, subject))
    except Exception as exc:  # noqa: BLE001
        logger.warning("共享上下文读取失败(已忽略): %s:%s %s", kind, subject, exc)
        return None


def recent_entries(kind: str = KEY_DIAGNOSIS, limit: int = 5) -> list[dict]:
    """按 kind 取最近若干条共享上下文(整条,含 source_agent/correlation_id)。

    这是**卖家画像注入共享上下文的读路径**:参谋写下的诊断要能被下一轮的参谋
    自己、以及营销读到,否则这个池子就只是个写完没人看的审计表。按 kind 前缀取
    最近 N 条,而不是按 subject 精确取(fetch/fetch_entry 那条路)——店主开口时
    我们并不知道他要问哪个商品,给最近的几条让模型自己挑更贴近实际。

    与 share()/fetch() 同样 fail-soft:读不到就返回空列表,渲染出来是空串,
    卖家那一轮照常进行。
    """
    from app.config.settings import settings

    if not getattr(settings, "collab_enabled", True):
        return []
    if _use_redis():
        return _redis_recent_entries(kind, max(1, int(limit)))
    try:
        return get_db().list_shared_context(
            prefix=f"{kind}:", limit=max(1, int(limit))
        )
    except Exception as exc:  # noqa: BLE001 读不到就不注入,绝不打断卖家会话
        logger.warning("共享上下文列举失败(已忽略,本轮不注入): %s %s", kind, exc)
        return []


def list_shared_context(
    prefix: str = "",
    limit: int = 50,
    correlation_id: Optional[str] = None,
) -> list[dict]:
    """按 key 前缀列共享上下文(兼容 SQLite Database 接口,供管理端点使用)。

    Redis 后端时从 prefix 提取 kind,走 Sorted Set 索引取最近条目;
    correlation_id 过滤在 Python 层完成(Redis 无此索引,但管理端点的数据量
    很小,逐条过滤完全可接受)。
    """
    if _use_redis():
        kind = prefix.rstrip(":") if prefix else ""
        entries = _redis_recent_entries(kind, limit)
        if correlation_id and entries:
            entries = [
                e for e in entries if e.get("correlation_id") == correlation_id
            ]
        return entries
    return get_db().list_shared_context(
        prefix=prefix, limit=limit, correlation_id=correlation_id
    )


# ---- 渲染(纯函数,不涉及存储后端)----


def _serialize_value(value: dict) -> str:
    """把 value 序列化成围栏里的一行文本,用 JSON 而非隐式 dict repr / str()。

    两个原因,后一个才是硬约束:
    1) 可读性——JSON(ensure_ascii=False)对中文更友好,repr() 里的 `'...'`
       和转义符对下游(中文模型)读起来更别扭。
    2) 围栏完整性——json.dumps 会把字符串里的真实换行符转义成字面 `\\n`,
       不会在渲染文本里产生新的物理线。dict repr 恰好也有这个副作用,但那
       只是 Python 实现细节的意外,不是设计约定;换成 str(value) 这层保护
       就没了。只要 value 里的换行能原样进入输出,内容就能在视觉上"提前"
       伪造出一行新的【共享上下文结束】,让人工审阅者或不够警惕的模型把
       伪造的结束标记当真,从而把标记之后本该被当成数据的原文当成指令。
       所以这里的转义不是可有可无的实现细节,是围栏这道第一层防线成立的
       前提。
    """
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        logger.warning("共享上下文 value 无法 JSON 序列化,退化为 repr: %r", value)
        return repr(value)


from prompts import get as _get_prompt

_DATA_FENCE_TEMPLATE = _get_prompt("shared_context/data_fence")


def render_context_block(entries: list[dict]) -> str:
    """把共享上下文渲染成注入 prompt 的片段,**正文加数据围栏**。

    围栏不是万能的(内容里可以伪造结束标记,围栏只是第一层防线,不是保证);
    真正的兜底是这些内容只 influence 参谋/营销的**建议文本**,而营销的产物
    必过人工审批,参谋的工具全只读——即便被注入也无法触发任何写动作。

    对畸形条目(缺 key / source_agent,或 value 不是 dict)选择**跳过**而不
    是硬凑占位符渲染出来:本模块唯一的写入口 share() 保证 value 恒为 dict,
    一条 entry 连这个最基本的形状都不满足,大概率是数据损坏或未来新调用方
    的 bug,而不是"合法的空诊断"——渲染成任何看起来像内容的文本(包括
    `None`)都可能被参谋/营销误读成真实结论。跳过并记 warning,和
    share()/fetch() 现有的 fail-soft 风格一致:调用方主流程不受影响,问题
    留在日志里可查,不会污染进 LLM 看到的上下文块。
    """
    if not entries:
        return ""
    entry_lines: list[str] = []
    for e in entries:
        key = e.get("key")
        source_agent = e.get("source_agent")
        value = e.get("value")
        if not key or not source_agent or not isinstance(value, dict):
            logger.warning("跳过格式错误的共享上下文条目: %r", e)
            continue
        entry_lines.append(f"- [{source_agent}] {key}: {_serialize_value(value)}")
    if not entry_lines:
        return ""
    return "\n\n" + _DATA_FENCE_TEMPLATE.format(entries="\n".join(entry_lines))
