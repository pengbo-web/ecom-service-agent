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
import threading
import uuid
from typing import Callable, Optional

from app.multi_agent.event_bus import get_event_bus

logger = logging.getLogger(__name__)

#: 保护 consume() 的 stats 字典。L1 事件间并行之后多个线程会同时自增它,
#: 而 dict 的 += 是读-改-写、不原子——丢一次计数就等于悄悄少报一次失败。
_stats_lock = threading.Lock()

# Agent 标识(同时是 target_agent 的取值域)
AGENT_SERVICE = "service"     # 客服服务 Agent(C 端)
AGENT_ANALYST = "analyst"     # 店铺参谋 Agent(B 端只读)
AGENT_GROWTH = "growth"       # 营销增长 Agent(B 端草稿型)
AGENT_HUMAN = "human"         # 人工闸:需要人来看的事件投给它
# 风控/静默节点(B 端,只做**负向**动作):收到经营异常时暂停对应商品的推广。
# 单独立成一个节点而不是塞进 growth,是因为它与 growth 的取向相反——growth 决定
# "多做一件事"(起草触达)、guard 决定"少做一件事"(停掉推广)。合在一个消费者里,
# "起草失败"与"静默失败"会共用同一条事件记录的 status,一个失败会连带另一个被
# 重投,而它们的重试语义完全不同(起草可以重来,静默重复写是幂等的)。
AGENT_GUARD = "guard"

#: 事件的终止状态之一:**发布时没有任何订阅者**。
#:
#: 与 done/failed 并列,补的是状态机原本表达不了的第三种结局——"这条链正当地
#: 走到了尽头"。done = 有人处理完了;failed = 有人试了但炸了;
#: no_subscriber = 压根没人订阅,而这**不是故障**(一个暂时没人订阅的事件是
#: 完全合法的)。
#:
#: 刻意不做成 pending:它不是待办,没人会来认领它。也刻意不复用 done:
#: 那会让"处理完了"和"没人处理"在统计里合成一件事,而它们的处置完全不同。
STATUS_NO_SUBSCRIBER = "no_subscriber"

# 事件类型
EV_SIGNAL_ANOMALY = "signal.anomaly"        # 客服侧/扫描器发现异常
EV_INSIGHT_DIAGNOSIS = "insight.diagnosis"  # 参谋出诊断结论
EV_DRAFTS_READY = "action.drafts_ready"     # 营销出好草稿,待人工审批
EV_OUTREACH_SENT = "result.outreach_sent"   # 人工批准并发出,闭环回写
EV_OUTREACH_CONVERTED = "result.outreach_converted"  # 归因 worker:触达后订单状态向前推进
EV_OUTREACH_NO_CHANGE = "result.outreach_no_change"  # 归因 worker:触达后没有向前推进


def new_correlation_id(prefix: str = "C") -> str:
    """一条协作链的 id。用 uuid4 而非时间戳:同一秒可能起多条链。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def publish(event_type: str, payload: dict, source: str,
            target: Optional[str] = None,
            correlation_id: Optional[str] = None) -> Optional[str]:
    """发布事件,返回这条协作链的 correlation_id;失败或无人订阅返回 None。

    **发布方只宣布"发生了什么",不指定收件人。** `target` 留空时由总线层的
    路由表(app/multi_agent/routing.py)决定投给谁——这是"Expert Agent 之间
    没有一方在指挥另一方"的落地方式:参谋不再在自己的处理器里写死"这条诊断
    要转给营销",它只宣布"我出了一条诊断"。

    **扇出发生在写入时**:路由表算出 N 个订阅者就插 N 行投递记录(同一
    correlation_id、同一 payload、不同 target_agent)。这样幂等认领、失败
    隔离、滞留回收全部沿用现有机制一行不改——每条投递记录就是今天的一条
    事件。另一种做法(只存一行、消费时按订阅反查)会直接破坏幂等:`status`
    是行级单值,参谋处理完置 done,营销就再也捞不到了。

    显式传 `target` 时**跳过路由表直投**,行为与改造前逐字节一致。保留它
    不是为了照顾现有调用方(它们都已改成不传),而是留一个逃生口:将来若
    出现"这条事件就是要点对点给某个 Agent"的场景,不必为它扭曲路由表。

    **返回 None 有两种含义**:发布失败,或**没有任何订阅者**。两者在调用方
    看来都是"这条链没起来"(现有调用方也只拿它做真值判断),故合并;但不要
    据此认为 None 一定是故障——一个暂时没人订阅的事件是完全合法的。

    **fail-soft**:发布点之一在买家会话的热路径上(客服 Agent 轮末埋点),
    总线不可用绝不能让买家那一轮失败,所以异常一律吞掉记日志——与既有
    skill_trace 埋点同一姿态。
    """
    from app.config.settings import settings

    if not getattr(settings, "collab_enabled", True):
        return None
    corr = correlation_id or new_correlation_id()
    try:
        # 事件标准校验:缺必需字段**只记 warning、照常发布**。
        # 发布点之一在买家会话的热路径上(streaming.py 的转人工埋点),一条埋点
        # 的 schema 问题绝不该让买家那一轮失败。真正的保障是测试
        # (test_collab_event_schema.py 钉住"路由谓词读的字段必须已声明"),
        # 运行时校验只负责在漏发真的发生时留下一条能被搜到的线索——因为这类
        # 失败的形态是"什么都没发生",没有日志就等于没有线索。
        from app.multi_agent.event_schema import missing_fields
        _missing = missing_fields(event_type, payload or {})
        if _missing:
            logger.warning("协作事件缺少必需字段(仍照常发布) event=%s missing=%s corr=%s",
                           event_type, _missing, corr)

        if target is not None:
            targets = [(target, 0)]
        else:
            from app.multi_agent.routing import resolve
            targets = resolve(event_type, payload or {})
        if not targets:
            # **落一条墓碑,不静默返回。**
            #
            # 改造前这里只有一句 `logger.debug` 就 return 了,后果是实测到的:
            # 库里有 462 条 tool_error_rate_high、288 条 service_escalation 信号,
            # 参谋**确实**都归因了(shared_context 里躺着 diagnosis:track-order),
            # 但这些诊断发出来时没有下游订阅者,于是**事件表里一行都没有**。
            # 排查时只能靠 shared_context 反推参谋到底干没干活——而一条链"正当地
            # 走到了尽头"与"根本没跑起来"在事件表上长得一模一样。
            #
            # 状态机原本只能表达"跑完了(done)"和"炸了(failed)",表达不了
            # "合法地没往下走"。`no_subscriber` 补的就是这一格。
            #
            # target_agent 写空串:这条记录**没有**收件人,那正是它要说的事。
            # 空串进不了 claim(没有 Agent 叫 ''),也进不了 reclaim/failed
            # (状态不对),所以它对既有工作流零影响——详见 publish_event。
            logger.info("协作事件无订阅者,已落终止记录 event=%s corr=%s",
                        event_type, corr)
            try:
                get_event_bus().publish(event_type, payload or {}, source, "",
                                        corr, priority=0, status=STATUS_NO_SUBSCRIBER)
            except Exception as exc:  # noqa: BLE001 墓碑写不下去不该影响调用方
                logger.warning("无订阅者终止记录写入失败(已忽略) corr=%s: %s", corr, exc)
            # 仍然返回 None:调用方的语义是"这条链有没有起来",而它确实没起来。
            # 墓碑是给**人**看的,不改变调用方的判断——现有调用方都只拿返回值
            # 做真值判断,改成返回 corr 会让"发出去了"和"没人接"混成一件事。
            return None
        eb = get_event_bus()
        for t, prio in targets:
            eb.publish(event_type, payload or {}, source, t, corr, priority=prio)
        return corr
    except Exception as exc:  # noqa: BLE001 旁路埋点,绝不影响主链路
        logger.warning("协作事件发布失败(已忽略) corr=%s: %s %s", corr, event_type, exc)
        return None


def _finish_and_count(eb, event_id: int, intended_status: str, stats: dict) -> None:
    """收尾一条事件并按**实际落库结果**计数,而不是按"处理器跑完了"计数。

    finish_event 只在事件仍是 processing 时才生效,有两类"没真正落库"的
    情况,都不能被当成 done/failed 计入统计,否则统计会撒谎:

    - 抛异常:锁表、磁盘满、连接断开等持久化层故障,原地兜住不外泄,
      记 warning(带 event id 与意图状态)方便定位;
    - 返回 False:该行已不在 processing(比如被并发的
      reclaim_stale_events 抢先收回成 pending),同样不能算数。

    这两种情况统一计入 stats["persist_failed"](按需惰性创建这个 key,
    正常路径下 stats 仍只有 claimed/done/failed 三个 key,不影响既有调用方
    和测试的字典等值断言)。不重试——这条事件是否要重来,交给
    reclaim_stale_events 或人工决定,consume 本身不做二次尝试。
    """
    try:
        persisted = eb.finish(event_id, intended_status)
    except Exception as exc:  # noqa: BLE001 落库失败不能拖垮整批,也不能外泄
        logger.exception(
            "协作事件收尾落库异常 id=%s intended_status=%s: %s",
            event_id, intended_status, exc)
        persisted = False
    else:
        if not persisted:
            logger.warning(
                "协作事件收尾未生效(该行已不在 processing,大概率被并发的 "
                "reclaim_stale_events 抢先收回)id=%s intended_status=%s",
                event_id, intended_status)

    # L1 并行之后 stats 会被多个线程写。dict 的 `+=` 不是原子操作(读-改-写),
    # 并发下会丢计数——而这份统计正是 worker 唯一的产出信号,少算一条就等于
    # 悄悄少报了一次失败。用一把模块级锁保护;它只圈住几次整数自增,不含 IO。
    with _stats_lock:
        if persisted:
            # 用 get 而不是直接 += :`skipped` 这类状态是惰性创建的(见 consume 里
            # stats 的初始化注释),直接 += 会 KeyError。
            stats[intended_status] = stats.get(intended_status, 0) + 1
        else:
            stats["persist_failed"] = stats.get("persist_failed", 0) + 1


def consume(target: str, handler: Callable[[dict], None], limit: int = 20) -> dict:
    """认领并处理该 Agent 的待处理事件,返回 {claimed, done, failed[, persist_failed]}。

    单个处理器抛异常只把**那一条**置 failed,不影响同批其它事件——一个坏事件
    不能卡死整条流水线。failed 的事件不会被自动重试(避免坏事件无限循环),
    留在表里供人工在时间线上看到并决定。

    收尾(finish_event)本身也可能失败(见 _finish_and_count):这同样只能
    算作那一条事件的问题,不能让异常逃出 consume() 摁停整个 worker 循环、
    抛下同批其它已认领的事件晾在 processing 里不管。
    """
    from app.config.settings import settings

    # skipped 惰性创建(与 persist_failed 同姿态):没被拦过的轮次里 stats 仍只有
    # claimed/done/failed 三个键,既有调用方与测试的字典等值断言不受影响。
    stats = {"claimed": 0, "done": 0, "failed": 0}
    if not getattr(settings, "collab_enabled", True):
        return stats
    try:
        eb = get_event_bus()
        events = eb.claim(target, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("协作事件认领失败: %s", exc)
        return stats

    stats["claimed"] = len(events)
    from app.multi_agent.routing import check_gate

    def _one(ev: dict) -> None:
        """处理一条事件(闸 → handler → 收尾)。所有计数都经 `_finish_and_count`,
        它对 stats 的写入由下面的锁保护。"""
        # 状态拦截:事件已经投递到了,但**此刻**该不该动手,由消费闸判
        # (见 routing.GATES)。放在这里而不是发布侧的两个理由:发布点在买家
        # 热路径上不能读库;更要紧的是状态在发布与消费之间会变——事件可能在
        # 队列里躺了整整一个 worker 间隔,发布时判等于拿过期状态做决定。
        allowed, gate_reason = check_gate(target, ev)
        if not allowed:
            # 落 `skipped` 而不是 done/failed:
            #   不是 done —— 它并没有被处理,记成 done 就是账本撒谎;
            #   不是 failed —— 它没有出错,混进失败列表会淹没真正的故障。
            # skipped 不会被 claim_events(只挑 pending)再捞到,也不进
            # list_failed_events,但在协作时间线上如实可见。
            logger.info("消费闸拦截 id=%s type=%s target=%s: %s",
                        ev.get("id"), ev.get("event_type"), target, gate_reason)
            _finish_and_count(eb, int(ev["id"]), "skipped", stats)
            return
        try:
            handler(ev)
        except Exception as exc:  # noqa: BLE001 单条失败不拖垮整批
            logger.exception("协作事件处理失败 id=%s type=%s: %s",
                             ev.get("id"), ev.get("event_type"), exc)
            _finish_and_count(eb, int(ev["id"]), "failed", stats)
            return
        _finish_and_count(eb, int(ev["id"]), "done", stats)

    # L1 事件间并行:一批认领到的事件彼此**没有数据依赖**(各自 correlation_id
    # 不同、各自独立收尾),而每条都含一次 LLM 调用(超时上限 30s)——串行时
    # limit=20 最坏 600s。
    #
    # 与 L2(起草并行)的关系:L2 并行的是"一条诊断下的 N 个商机",L1 并行的是
    # "N 条事件"。两层可以叠加,但并发度会相乘,所以 L1 复用同一个
    # `collab_max_parallel` 上限而不是各设一个——8×8=64 个线程同时写 SQLite
    # 只会让它们互相等锁,比串行还慢。
    #
    # 认领(claim)仍是串行的一次原子操作,不参与并行:它本来就快,而且并行认领
    # 需要的幂等保证已经由条件更新提供,没有必要再拆。
    if not settings.collab_parallel_enabled or len(events) <= 1:
        for ev in events:
            _one(ev)
    else:
        import concurrent.futures
        workers = min(max(1, int(settings.collab_max_parallel)), len(events))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="collab-consume") as pool:
            list(pool.map(_one, events))
    return stats


# ---------------------------------------------------------------------------
# 观测与人工干预出口。
# ---------------------------------------------------------------------------
#
# 这四个函数是本轮补上的:改造前协作时间线、失败列表、手动重试、滞留回收
# **绕过本模块直接调 `get_db().xxx_events(...)`**。也就是说"换消息中间件只改
# 一个文件"这句话对三个 Agent 是真的,对观测与管理路径是假的——那四处会全断。
# 收进来之后,总线的载体边界才真的只有 `event_bus.EventBus` 一个面。

def timeline(correlation_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    """一条协作链上的全部事件(不传 correlation_id 则取最近若干条)。

    这是"多 Agent 到底协作了什么"唯一可验证的出口——没有它,协作就只是一句
    宣称。换载体时这个能力必须保住:Kafka 本身按字段查不了,那天要靠投影表。
    """
    return get_event_bus().timeline(correlation_id=correlation_id, limit=limit)


def failed(limit: int = 50) -> list[dict]:
    """处理失败的事件。系统刻意不自动重试(坏事件会无限循环),所以它们必须
    看得见,否则"留在表里供人工决定"等于留给没人。"""
    return get_event_bus().failed(limit=limit)


def failed_count() -> int:
    return get_event_bus().failed_count()


def retry(event_id: int) -> bool:
    """把一条失败事件放回待处理队列。条件更新,连点两次只有第一次生效。"""
    return get_event_bus().retry(event_id)


def reclaim_stale(older_than_seconds: int = 300) -> int:
    """回收滞留在 processing 超时的事件(救"worker 认领后崩在半路")。"""
    return get_event_bus().reclaim_stale(older_than_seconds=older_than_seconds)
