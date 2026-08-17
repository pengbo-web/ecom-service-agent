"""总线层路由表:决定「什么事件该给谁处理」。

这是把**编排决策**从各个 Expert Agent 里搬出来的地方。

改造前:发布方在 `publish(..., target=AGENT_GROWTH)` 里直接指定收件人,而
"什么样的诊断该转给营销"这条规则写死在参谋的处理器代码里(`MARKETING_WORTHY`)。
后果不是理论上的:
  - 想加一个订阅同一条信号的新 Agent,得去改客服会话热路径与扫描器的发布调用,
    而那两处与新 Agent 毫无关系;
  - `target_agent` 是单值,同一条异常信号天然做不到同时给两个 Agent;
  - 决定"营销 Agent 何时被唤醒"的规则藏在参谋模块的常量里,运营看不到。

改造后:发布方只宣布"发生了什么",收件人由本表决定。三个 Expert 之间因此
**没有任何一方在指挥另一方**——它们只是各自订阅了自己关心的事件。

**路由表是调度,不是授权。** 它决定谁被唤醒,永远不决定谁被允许做什么——
能力边界仍然由工具子集(各画像独立 ToolManager)、consent 门、人工审批闸强制。
往这里加一条订阅**不会**让任何 Agent 多出一分权限。

为什么是代码常量而不是数据库配置表:订阅条件是带业务语义的谓词(如"归因降级
的诊断不转营销"),不是能塞进 SQL 的标量比较;硬做成表就要发明一套条件 DSL。
而这份表改动频率极低(加 Agent 才动),每次改动都该配一次回归测试——正是代码
而非配置的适用场景。"可配置"的价值在于**集中且可见**,不在于能热改。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from app.multi_agent.bus import (AGENT_ANALYST, AGENT_GROWTH, AGENT_GUARD,
                                 AGENT_HUMAN, EV_DRAFTS_READY,
                                 EV_INSIGHT_DIAGNOSIS, EV_OUTREACH_CONVERTED,
                                 EV_OUTREACH_NO_CHANGE, EV_OUTREACH_SENT,
                                 EV_SIGNAL_ANOMALY)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Subscription:
    """一条订阅:某个 Agent 关心某类事件,可带条件谓词与优先级。

    when: `(payload) -> bool`;None = 无条件订阅。谓词必须是**纯函数**
    (不读库、不调模型)——路由发生在发布的那一刻,而发布点之一在买家会话的
    热路径上,任何 IO 都会变成买家那一轮的延迟。需要读状态的规则请放到
    **消费闸**(见本模块 `GATES`),那里在 worker 里跑,不碰热路径。

    priority: 常量 int 或 `(payload) -> int`,越大越先被认领(同值仍 FIFO)。
    支持谓词形式是必要的:本项目所有异常都走同一个 `signal.anomaly` 事件类型,
    "买家刚被转人工"与"例行退款率扫描"的区别在 payload 的 `kind` 里,不在
    事件类型上——只支持常量就表达不了这个差异。
    """

    target: str
    when: Optional[Callable[[dict], bool]] = None
    priority: "int | Callable[[dict], int]" = 0
    #: 给人看的规则说明,出现在日志与后续可能的管理端展示里。
    reason: str = ""


def _marketing_worthy(payload: dict) -> bool:
    """哪些诊断值得唤醒营销 Agent。

    这条规则原本写死在 `collab.py` 的 `handle_signal` 里(模块常量
    `MARKETING_WORTHY` + 一个 `if`),搬到这里而不是复制过来。

    两个条件:

    1. `kind` 必须是与成交转化直接相关的异常。当前只有商品退款率跨线——
       它的下游动作(给受影响的滞留订单发触达)有明确因果;而工具失败率、
       转人工率这些是服务质量问题,推销不解决它们。
    2. `degraded` 的诊断不转。归因不可用时 `conclusion` 只剩一句"仅列事实、
       归因暂不可用",而 `_llm_draft` 会把它原文嵌进买家话术——这种半成品
       文案写给买家没有意义。人工审批挡得住"内容有没有问题",挡不住"这条
       消息压根不该被生成";而等下一轮扫描重来的成本很低。
    """
    if payload.get("degraded"):
        return False
    return payload.get("kind") in _MARKETING_WORTHY_KINDS


#: 值得唤醒营销的异常类型。与 `AUTONOMOUS_DRAFT_KINDS`(营销扫哪些商机)是
#: 两件事:这里说的是"什么异常配得上一次营销动作",那里说的是"被唤醒之后去
#: 捞哪几类人"。
_MARKETING_WORTHY_KINDS = frozenset({"refund_rate_high"})


#: 优先级档位。只设三档,不搞 0-100 的连续值——连续值会让每次新增订阅都要
#: 纠结"填 37 还是 42",而实际决策从来只有"更急/一般/可以等"三种。
P_URGENT = 10   # 有真人正在等(当前只有:买家刚被转人工)
P_NORMAL = 0    # 后台协作的默认档
P_LOW = -10     # 纯统计回写,晚一轮没有任何影响


def _anomaly_priority(payload: dict) -> int:
    """异常信号的优先级:转人工的比例行扫描更该先看。

    `service_escalation` 是买家刚刚被转人工时客服侧发的旁路信号——它背后有一个
    真人正卡在那里。而退款率/差评率跨线是定时扫描的产物,晚一轮完全无所谓。
    两者共用 `signal.anomaly` 这一个事件类型,区别只在 payload 的 kind 上,
    所以优先级只能用谓词表达。
    """
    return P_URGENT if payload.get("kind") == "service_escalation" else P_NORMAL


#: 哪些异常的 `subject` 是**商品 SKU**——只有这些才谈得上"暂停推广该商品"。
#:
#: 这张表必须按 subject 的**语义**来定,不能按"听起来严重不严重":
#:   refund_rate_high / bad_review_rate_high  → subject 是 sku      ✅
#:   tool_error_rate_high / human_rate_high   → subject 是 skill 名 ❌
#:   service_escalation                       → subject 是会话 id   ❌
#:   angry_rate_high                          → subject 是 "shop"   ❌
#: 放错一个,静默记录的 subject 就会是个技能名或会话号,而它要跟
#: `order_items.sku` 比较——比较永远不成立,于是这条闸变成永远不触发的死代码,
#: **不报错、不留日志**。这个仓库在 `render_buyer_hints` 上已经栽过一次同样
#: 形态的跤,所以宁可把判据写死在表里,也不用"kind 里带 rate 就算"这种推断。
_PAUSE_WORTHY_KINDS = frozenset({"refund_rate_high", "bad_review_rate_high"})


def _pause_worthy(payload: dict) -> bool:
    """这条异常信号该不该触发商品级推广静默。**纯函数**(不读库、不调模型)。

    只看 kind 与 subject 两个字段:kind 在白名单里,且 subject 非空(拿不到商品
    就无从静默)。**不在这里判"静默功能开没开"**——那是
    `settings.collab_promotion_pause_hours`,读它意味着路由结果随配置漂移,而
    路由发生在发布那一刻、写进事件表就固定了。开关放在处理器里,事件照投、照
    消费、照留痕,关掉时只是不写那条会拦人的记录(见 `collab.handle_guard`)。
    """
    if str(payload.get("kind") or "") not in _PAUSE_WORTHY_KINDS:
        return False
    return bool(str(payload.get("subject") or "").strip())


#: 事件类型 → 订阅者列表。**新增一个 Agent 只需在这里加一行。**
SUBSCRIPTIONS: dict[str, list[Subscription]] = {
    # 客服侧埋点与确定性扫描发出的异常 → **同时**投给参谋和风控两个节点。
    #
    # 这是本表里唯一一处真正的扇出(在此之前 6 个事件 6 条订阅,全是 1:1,
    # 扇出/谓词/优先级三套机制里只有优先级有真实用户)。两个分支并行、互不
    # 依赖、失败隔离:
    #   参谋 → 想明白"为什么"(要调 LLM,慢,可能降级)
    #   风控 → 立刻"少做一件事"(纯确定性写库,快,不该等归因结论)
    #
    # 顺序上风控**不能**排在参谋后面:等归因跑完(实测几十秒)才暂停推广,那几十
    # 秒里正好可能有人点批准把这条推广发出去。负向动作要抢在前面,这也是给它
    # P_URGENT 的理由——它不需要更"重要",它需要更"早"。
    EV_SIGNAL_ANOMALY: [
        Subscription(AGENT_ANALYST, priority=_anomaly_priority,
                     reason="所有异常信号都由参谋归因;转人工类优先"),
        Subscription(AGENT_GUARD, when=_pause_worthy, priority=P_URGENT,
                     reason="商品级经营异常先暂停该商品推广,不等归因结论"),
    ],
    # 参谋的诊断 → 营销(带条件)
    EV_INSIGHT_DIAGNOSIS: [
        Subscription(AGENT_GROWTH, when=_marketing_worthy,
                     reason="仅转化相关且归因未降级的诊断才唤醒营销"),
    ],
    # 营销起好草稿 → 人工闸(这是唯一的发送出口,永远指向人)
    EV_DRAFTS_READY: [
        Subscription(AGENT_HUMAN, reason="草稿必须经人工审批才会发出"),
    ],
    # 人工批准并投递 → 参谋(闭环回写,供后续统计)
    EV_OUTREACH_SENT: [
        Subscription(AGENT_ANALYST, reason="触达已发生,回写给参谋做效果统计"),
    ],
    # 归因结果 → 人工(供工作台展示;暂无自动消费方)
    EV_OUTREACH_CONVERTED: [
        Subscription(AGENT_HUMAN, priority=P_LOW,
                     reason="转化结果供人工在工作台查看"),
    ],
    EV_OUTREACH_NO_CHANGE: [
        Subscription(AGENT_HUMAN, priority=P_LOW,
                     reason="未转化结果供人工在工作台查看"),
    ],
}


def _priority_of(sub: Subscription, payload: dict) -> int:
    """算这条订阅的优先级。谓词出错回落 P_NORMAL——排序算不出来不该让事件发不出去。"""
    if not callable(sub.priority):
        return int(sub.priority)
    try:
        return int(sub.priority(payload or {}))
    except Exception:  # noqa: BLE001
        logger.warning("优先级谓词异常,按普通优先级处理 target=%s", sub.target,
                       exc_info=True)
        return P_NORMAL


def resolve(event_type: str, payload: dict) -> list[tuple[str, int]]:
    """算出这条事件该投递给谁、各自什么优先级。纯函数,无 IO。

    返回 `[(target, priority), ...]`。

    未登记的 event_type 返回 `[]` 而**不报错**:发布一个暂时没人订阅的事件是
    合法的。"没人关心"与"配置漏了"在运行时无法区分,报错只会让"引入一个新
    事件类型"变成一次线上故障——而这恰恰是这套架构应该让它变容易的事。

    条件谓词抛异常时**该订阅不投递**并记 warning(fail-closed):判不清就不唤醒。
    多唤醒一个 Agent 的代价是它可能对着半成品数据产出垃圾草稿,而漏唤醒的代价
    只是这一轮没跑——两者不对称,所以选择不唤醒。

    注意这与下面 `check_gate` 的 fail-**open** 方向相反,不是前后矛盾:这里
    决定的是"要不要凭空造出一次 Agent 唤醒",那是增量动作,判不清就不做;
    那里决定的是"要不要拦下一件已经该做的事",拦截是减量动作,判不清就别拦。
    """
    out: list[tuple[str, int]] = []
    for sub in SUBSCRIPTIONS.get(event_type, ()):
        if sub.when is not None:
            try:
                if not sub.when(payload or {}):
                    continue
            except Exception:  # noqa: BLE001 判不清就不唤醒
                logger.warning(
                    "订阅条件判定异常,本条不投递 event=%s target=%s rule=%s",
                    event_type, sub.target, sub.reason, exc_info=True)
                continue
        out.append((sub.target, _priority_of(sub, payload or {})))
    return out


# ---------------------------------------------------------------------------
# 消费闸(状态拦截):事件已经投递到了,但**此刻**要不要真的处理它。
# ---------------------------------------------------------------------------
#
# 与订阅条件(上面的 `when`)的分工:
#   订阅条件 —— 发布那一刻判,纯内存,回答"这类事件该不该给这个 Agent"
#   消费闸   —— 消费那一刻判,可读库,回答"**此刻**这个 Agent 该不该动手"
#
# 为什么状态判断必须放在消费侧:①发布点之一在买家会话热路径上,读库会变成
# 买家那一轮的延迟;②更要紧的是**状态在发布与消费之间会变**——发布时判等于
# 拿过期状态做决定,而事件在队列里可能躺了一整轮 worker 间隔。


def _marketing_paused(_event: dict) -> tuple[bool, str]:
    """营销静默期:服务侧正在救火时,不同时推销。

    判据是未结人工工单数——一堆买家正等着人工处理,说明店铺当下的处境是
    "先把火灭了",此时启动一轮营销起草是明显的时机错误。用的是已有数据
    (`HandoffQueue.count_pending()`),不新增任何采集。

    为什么不用 `arbitration.check_outreach_allowed`:那条是**按买家**判的
    (这个买家是否正在被人工处理),而 `insight.diagnosis` 的 payload 里
    subject 是**商品**,没有 user_id,按买家的仲裁在这一跳上无从下手。它已经
    在投递侧(approve_draft)与跟进侧生效,位置是对的;这里需要的是一条
    **店铺级**的状态规则,两者互补而非重复。

    阈值 0 = 关掉这条闸。
    """
    from app.config.settings import settings

    limit = int(getattr(settings, "collab_marketing_pause_open_handoffs", 0) or 0)
    if limit <= 0:
        return True, ""
    from app.hitl.queue import HandoffQueue

    pending = HandoffQueue(settings.hitl_db_path).count_pending()
    if pending >= limit:
        return False, f"服务侧有 {pending} 条未结人工工单(阈值 {limit}),营销静默"
    return True, ""


#: target agent → 消费闸。未登记的 agent 无闸,一律放行。
GATES: dict[str, Callable[[dict], tuple[bool, str]]] = {
    AGENT_GROWTH: _marketing_paused,
}


def check_gate(target: str, event: dict) -> tuple[bool, str]:
    """该 Agent 此刻能不能处理这条事件。返回 (allow, 中文原因)。

    **fail-open**:闸自己出错一律放行。这道闸是"少做一点事"的优化,不是安全
    边界——真正的安全边界是人工审批闸,它在后面且从不失效。反过来 fail-closed
    会让一次读库抖动静默吞掉整条协作链,那个代价大得多。
    """
    gate = GATES.get(target)
    if gate is None:
        return True, ""
    try:
        return gate(event)
    except Exception:  # noqa: BLE001 闸坏了就放行,让下游既有约束接着守
        logger.warning("消费闸判定异常,放行 target=%s", target, exc_info=True)
        return True, ""


#: 优先级档位 → 中文标签。渲染给人看时用,唯一口径在这里,前端不另抄一份。
PRIORITY_LABELS: dict[int, str] = {
    P_URGENT: "紧急", P_NORMAL: "普通", P_LOW: "低",
}


def describe() -> list[dict]:
    """把路由表渲染成可读结构,供文档/管理端展示。

    存在的理由:这份表是"多 Agent 到底怎么协作"的权威声明,它必须能被人读到,
    而不是只能靠翻代码。与 `OPPORTUNITY_KINDS` 从权威表渲染进 prompt 是同一
    手法——不允许任何地方手抄第二份。

    `priority` 对谓词式优先级给不出单一数值——它按 payload 现算(同一个
    `signal.anomaly` 事件,转人工是紧急、例行扫描是普通)。这种情况下如实标
    `dynamic=True` 并把谓词的中文说明放进 `priority_note`,**不能挑一个档位
    糊弄过去**:管理端摆出一个"普通"会让人以为转人工也排在普通队列里,而那
    正好是这条谓词存在的理由。
    """
    rows: list[dict] = []
    for ev, subs in SUBSCRIPTIONS.items():
        for s in subs:
            dynamic = callable(s.priority)
            rows.append({
                "event_type": ev,
                "target": s.target,
                "conditional": s.when is not None,
                "reason": s.reason,
                "priority_dynamic": dynamic,
                "priority": None if dynamic else int(s.priority),
                "priority_label": ("按事件内容动态判定" if dynamic
                                   else PRIORITY_LABELS.get(int(s.priority),
                                                            str(s.priority))),
            })
    return rows


def describe_gates() -> list[dict]:
    """把消费闸渲染成可读结构。

    与 `describe()` 分开而不是合成一张表:订阅条件在**发布**那一刻判(纯内存,
    回答"这类事件该不该给这个 Agent"),消费闸在**消费**那一刻判(可读库,回答
    "此刻该不该动手")。摆在同一张表里会让人以为它们是同一种规则,而这两者
    连失败方向都是相反的——订阅 fail-closed,闸 fail-open。
    """
    return [
        {"target": t, "name": getattr(g, "__name__", "").lstrip("_"),
         "reason": (getattr(g, "__doc__", "") or "").strip().split("\n")[0]}
        for t, g in GATES.items()
    ]
