"""失败归因分层:只让**可修的**信号回流到 skill 自改进。

---

**为什么必须做。** 论文(SkillEvo)明确警告:没有归因筛选就驱动修订,会把
**不可修的信号错误编码成知识**,产出文档膨胀与事实冲突。

本仓库有这个形态的**实测证据**,而且很干净:

    skill_traces 里 track-order 的 tool_error 有 38 次,success 只有 15 次。
    翻开那些失败,41 条错误里绝大多数是同一句「未找到订单 ORD-20240115-001」。

    查库:ORD-20240115-001 **存在**,属于「小明」;而打出这些失败的 user_id 是
    ab0/ev3/trk5 这类压测与评测用户。auth_enabled=True,于是 `owned_order`
    返回 None,工具复用"未找到订单"话术(刻意不泄露订单存在性)。

    **那是归属校验在正确地拦住跨用户访问。** 它不是 skill 的问题。把它当成
    知识缺口去补,只会往 SKILL.md 里写一堆没用的东西,还要走灰度、占审批位。

改造前这条路上只有一道过滤(`trace_is_infrastructure_only`,只认"明说自己是
连接/超时/不可用"的失败)。本模块把它从 1 类推广到 3 类。

---

**能用确定性规则判的,绝不问模型。**

本仓库手里的信号足够硬,不需要让 LLM 去猜:

| 判据 | 依据 | 类别 |
|---|---|---|
| 全部失败都是连接/超时/不可用 | `is_infrastructure_failure` | capability_limit |
| 订单存在但不属于该 user_id | 直接查库 | capability_limit |
| 前置授权门未放行 | 轨迹里的 `need_confirm` 标记 | capability_limit |
| 会话只有一轮买家消息 | 归档会话 | evaluation_noise |
| 只有守卫拦截、没有真实失败 | `blocked` 标记 | evaluation_noise |
| 人工坐席接管并给出了回复 | `intent=human_agent` | knowledge_gap |

**归属校验那条为什么敢直接查库**:归因是**离线**的,不在回话路径上,读一次库
没有延迟顾虑。而它是唯一能把"未找到订单"这句话拆成"真没有"与"不是你的"两种
含义的办法——错误文案本身刻意做成了不可区分(不泄露订单存在性),在字符串层面
永远分不开。这一点 `execution_trace.is_infrastructure_failure` 的注释早就说过:
"一个坏掉的依赖如果返回的是一句像模像样的业务错误,在这一层无法区分"。

---

**第四类:判不出。**

论文分三类。这里多一个 `undetermined`,是刻意的:一条没命中任何规则、又没有
人工回复可对照的失败,我们**不知道**它属于哪一类。把它默认塞进 knowledge_gap
等于"不知道就当成要改",那正是论文警告的那件事;塞进 evaluation_noise 又会
把真实缺陷悄悄丢掉。

所以它单独成一类:**不回流,但如实报数**。这个数字大起来本身就是信号——说明
判据不够用了,该补的是判据,不是让自进化蒙着眼睛跑。

(与本仓库 `risk=None` 表示"判不了"而非"低危"是同一条:别把"不知道"当成结论。)
"""

from __future__ import annotations

from app.agent.skills.execution_trace import (
    OUTCOME_HANDOFF,
    trace_is_infrastructure_only,
)

#: 三类 + 一个"判不出"。只有 `knowledge_gap` 回流到自改进。
CATEGORY_KNOWLEDGE_GAP = "knowledge_gap"
CATEGORY_CAPABILITY_LIMIT = "capability_limit"
CATEGORY_EVALUATION_NOISE = "evaluation_noise"
CATEGORY_UNDETERMINED = "undetermined"

CATEGORIES = (CATEGORY_KNOWLEDGE_GAP, CATEGORY_CAPABILITY_LIMIT,
              CATEGORY_EVALUATION_NOISE, CATEGORY_UNDETERMINED)

#: 判定是谁做的。**必须跟着结论走**:一个规则判出来的 capability_limit 和一个
#: 模型猜出来的,可信度不是一回事,而界面上只显示类别的话两者长得一模一样。
METHOD_RULE = "rule"
METHOD_MODEL = "model"
METHOD_NONE = "none"        # 没判出来

#: 中文标签,给界面与 CLI 用(同一份,免得三处各写一套措辞)。
CATEGORY_LABELS = {
    CATEGORY_KNOWLEDGE_GAP: "知识缺口(可修,回流自改进)",
    CATEGORY_CAPABILITY_LIMIT: "能力/权限边界(不可修,不回流)",
    CATEGORY_EVALUATION_NOISE: "评测噪声(不构成证据,不回流)",
    CATEGORY_UNDETERMINED: "判不出(不回流,需补判据)",
}

#: 一条会话至少要有这么多轮买家消息才算能说明问题。少于此的失败里,大量是
#: 买家自己中断/没把诉求说完,不构成"skill 没搞定"的证据。
MIN_MEANINGFUL_TURNS = 2


def _failed_calls(trace: dict) -> list[dict]:
    """真实失败的工具调用。`blocked` 不算:守卫拦住跳步、模型随后补齐并成功,
    是守卫**起作用**而非本轮失败(与 `SkillTurn.outcome` 同一口径)。"""
    return [c for c in (trace.get("tool_calls") or [])
            if not c.get("ok") and not c.get("blocked")]


def _order_ids(calls: list[dict]) -> list[str]:
    """失败调用里带的订单号(归属校验判据的输入)。"""
    out = []
    for call in calls:
        oid = (call.get("args") or {}).get("order_id")
        if oid and str(oid) not in out:
            out.append(str(oid))
    return out


def _ownership_denied(trace: dict, calls: list[dict], db) -> str | None:
    """这些失败是不是归属校验拦下来的。是则返回证据串,否则 None。

    判据:订单**在库里存在**,但 `order["user"]` 不是这条轨迹的 user_id。
    这两个条件缺一不可——只看"库里有"会把"查到了但工具另有故障"也算进来。

    `auth_enabled=False` 时归属校验根本不生效,直接返回 None:那种部署下
    "未找到订单"就是字面意思,判成权限边界是错的。
    """
    from app.config.settings import settings

    if not settings.auth_enabled:
        return None
    uid = str(trace.get("user_id") or "").strip()
    if not uid:
        return None
    for oid in _order_ids(calls):
        try:
            order = db.get_order(oid)
        except Exception:  # noqa: BLE001 离线归因,库读不到就判不出,不抛
            return None
        if order and str(order.get("user") or "") != uid:
            return (f"订单 {oid} 存在但属于其他用户,当前会话 user_id={uid};"
                    "归属校验按设计拒绝并复用「未找到订单」话术(不泄露订单存在性)")
    return None


def _human_reply(archive: dict | None) -> str:
    if not archive:
        return ""
    from app.evaluation.trace_to_case import extract_human_reply

    try:
        return extract_human_reply(archive)
    except Exception:  # noqa: BLE001
        return ""


def _buyer_turn_count(archive: dict | None) -> int:
    if not archive:
        return 0
    from app.agent.skills.case_synthesis import split_turns

    return len(split_turns(archive.get("messages")))


def attribute(trace: dict, archive: dict | None = None, *, db=None,
              client=None, model: str = "") -> dict:
    """给一条失败轨迹归因。返回 `{category, method, rule, reason, evidence}`。

    规则按**从确定到不确定**排序,先命中先返回。顺序本身是判断的一部分:
    基础设施故障排在归属校验前面,因为一次超时里带的订单号说明不了任何事。

    `client`/`model` 可选。给了才会在"有人工回复"的分支上调一次 LLM 去分辨
    "人工答出了 AI 答漏的稳定事实"(真知识缺口)与"人工只是安抚/破例让步"
    (不是知识缺口,照抄进 SKILL.md 会把一次让步变成一条政策)。
    不给就退到较粗的规则判定,并在 method 里如实标成 rule。
    """
    if db is None:
        from app.db import get_db
        db = get_db()

    calls = _failed_calls(trace)

    # ① 全是基础设施故障 —— 一次代理抖动/上游超时,与流程文档毫无关系。
    if calls and trace_is_infrastructure_only(trace.get("tool_calls")):
        return _result(CATEGORY_CAPABILITY_LIMIT, METHOD_RULE, "infra_only",
                       "本轮全部工具失败都是连接/超时/服务不可用",
                       [str(c.get("error"))[:80] for c in calls])

    # ② 归属校验拒绝 —— **本项目最大的一类误判来源**,见模块 docstring。
    evidence = _ownership_denied(trace, calls, db)
    if evidence:
        return _result(CATEGORY_CAPABILITY_LIMIT, METHOD_RULE, "ownership_denied",
                       "归属校验拒绝跨用户访问(权限边界,不是 skill 缺知识)",
                       [evidence])

    # ③ 前置授权门未放行 —— 用户还没确认,工具按设计不执行。
    confirm = [c for c in calls if c.get("need_confirm")]
    if confirm:
        return _result(CATEGORY_CAPABILITY_LIMIT, METHOD_RULE, "consent_required",
                       "风险动作的前置授权门未放行(按设计不执行,等用户确认)",
                       [f"{c.get('name')}: {str(c.get('error'))[:60]}" for c in confirm])

    # ④ 只有守卫拦截、没有真实失败 —— 守卫**起作用**了,不是失败。
    blocked = [c for c in (trace.get("tool_calls") or []) if c.get("blocked")]
    if blocked and not calls and trace.get("outcome") != OUTCOME_HANDOFF:
        return _result(CATEGORY_EVALUATION_NOISE, METHOD_RULE, "guard_blocked_only",
                       "本轮只有工作流守卫拦截、没有真实工具失败",
                       [f"{c.get('name')}: {str(c.get('error'))[:60]}" for c in blocked])

    # ⑤ 会话太短 —— 买家没把诉求说完就走了,不构成"没搞定"的证据。
    turns = _buyer_turn_count(archive)
    if archive is not None and turns < MIN_MEANINGFUL_TURNS:
        return _result(CATEGORY_EVALUATION_NOISE, METHOD_RULE, "session_too_short",
                       f"会话只有 {turns} 轮买家消息(阈值 {MIN_MEANINGFUL_TURNS}),"
                       "更像买家自己中断而不是 skill 没搞定", [])

    # ⑥ 人工接管并给出了回复 —— 有 human reference 可对照,这是最可能的真缺口。
    human = _human_reply(archive)
    if human:
        if client is not None and model:
            judged = _judge_with_model(client, model, human, trace)
            if judged is not None:
                return judged
        return _result(CATEGORY_KNOWLEDGE_GAP, METHOD_RULE, "human_takeover",
                       "人工坐席接管并给出了回复,存在可对照的参考解法",
                       [human[:120]])

    # ⑦ 判不出。**不回流,但要报数**——见模块 docstring 最后一节。
    return _result(CATEGORY_UNDETERMINED, METHOD_NONE, "no_rule_matched",
                   "没有命中任何确定性判据,也没有人工回复可对照",
                   [str(c.get("error"))[:80] for c in calls])


def _result(category: str, method: str, rule: str, reason: str,
            evidence: list[str]) -> dict:
    return {"category": category, "method": method, "rule": rule,
            "reason": reason, "evidence": evidence,
            "flows_back": category == CATEGORY_KNOWLEDGE_GAP,
            "label": CATEGORY_LABELS[category]}


def _judge_with_model(client, model: str, human_reply: str, trace: dict) -> dict | None:
    """人工确实回了,但**回的是知识还是让步**——只有这一处值得花一次调用。

    典型的假知识缺口:人工说"这次给您破例全额退,下不为例"。那是一次**授权范围内
    的让步**,不是流程文档缺了什么。照抄进 SKILL.md 就把一次破例变成了一条政策,
    而 SKILL.md 是要被当作指令执行的。

    判不出/调用失败一律返回 None,由调用方退到规则判定 —— 归因失败绝不能
    编一个类别出来。
    """
    from prompts import get_or_empty

    prompt = get_or_empty("skills/failure_attribution")
    if not prompt:
        return None
    tools = ", ".join(str(c.get("name")) for c in _failed_calls(trace)) or "(无)"
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content":
                       f"失败的工具调用: {tools}\n人工坐席最终回复:\n{human_reply[:600]}"}],
        )
        verdict = (resp.choices[0].message.content or "").strip().lower()
    except Exception:  # noqa: BLE001 归因失败 = 退回规则判定,不编结论
        return None

    for category in (CATEGORY_KNOWLEDGE_GAP, CATEGORY_CAPABILITY_LIMIT,
                     CATEGORY_EVALUATION_NOISE):
        if category in verdict:
            return _result(category, METHOD_MODEL, "human_takeover_judged",
                           "对照人工回复判定(模型)", [human_reply[:120]])
    return None


# --------------------------------------------------------------------------
# 批量
# --------------------------------------------------------------------------

def partition(traces: list[dict], archives: list[dict] | None = None, *, db=None,
              client=None, model: str = "") -> dict:
    """给一批失败轨迹归因并分桶。

    返回 `{"by_category": {...}, "flows_back": [...], "counts": {...}, "details": [...]}`。
    `flows_back` 就是可以喂给 `improve_skill` 的那批 —— **只有 knowledge_gap**。
    """
    by_session = {a.get("session_id"): a for a in (archives or [])
                  if a.get("session_id")}
    buckets: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    details: list[dict] = []

    for trace in traces or []:
        archive = by_session.get(trace.get("session_id"))
        verdict = attribute(trace, archive, db=db, client=client, model=model)
        buckets[verdict["category"]].append(trace)
        details.append({"session_id": trace.get("session_id"),
                        "skill_name": trace.get("skill_name"), **verdict})

    return {
        "by_category": buckets,
        "flows_back": buckets[CATEGORY_KNOWLEDGE_GAP],
        "counts": {c: len(buckets[c]) for c in CATEGORIES},
        "details": details,
    }


def summarize(result: dict) -> str:
    """一行人话,给 CLI 与日志用。**每一类都报数,包括判不出的那一类。**"""
    counts = result["counts"]
    total = sum(counts.values())
    parts = [f"{CATEGORY_LABELS[c].split('(')[0]} {counts[c]}" for c in CATEGORIES]
    return (f"失败归因 {total} 条:" + " / ".join(parts)
            + f";其中 {counts[CATEGORY_KNOWLEDGE_GAP]} 条回流自改进")
