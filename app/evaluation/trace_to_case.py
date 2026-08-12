"""线上 Trace 回流成评估用例（问题对话沉淀为回归测试）。"""

from app.hitl.escalation import is_context_derived_reason
from app.utils.keyword_match import match_known_terms


def case_has_assertions(case: dict) -> bool:
    """这条用例断言了任何东西吗。

    一条什么都不断言的用例**永远通过**,却每次评估都要真跑一遍、花一次 token,
    还会把通过率往上抬——它比没有这条用例更糟。
    """
    return bool(case.get("expected_intent")
                or case.get("expected_keywords")
                or case.get("expected_tools")
                or case.get("expected_requires_human") is not None)


def expectation_suppressed(trace: dict) -> bool:
    """这条 trace 的转人工期望是被"不可复现"规则**主动去掉**的吗。

    用来把丢弃范围**收紧到本次改动自己造成的空壳**。反例(实测撞到):
    prompt injection 被拦截的 trace(`intent="blocked"`)本来就产不出任何断言——
    `blocked` 被排除在 expected_intent 之外、guard span 不是 tool span、没有 hitl。
    它在改动之前就是个空壳,**不是我弄空的**,而一条注入尝试的输入本身值得留着
    让人补期望(界面上它是"候选",不是已采纳的用例)。

    只有"有 hitl span、理由确实记着、且理由全是会话级"这一种,才是被规则去掉了
    期望的那批。
    """
    hitl_spans = [s for s in trace.get("spans", []) if s.get("kind") == "hitl"]
    reasons = [r for s in hitl_spans for r in (s.get("meta") or {}).get("reasons", [])]
    return bool(hitl_spans and reasons
                and all(is_context_derived_reason(r) for r in reasons))


def is_problem_trace(trace: dict) -> bool:
    if trace.get("status") == "error":
        return True
    if trace.get("intent") == "blocked":
        return True
    if any(s.get("kind") == "hitl" for s in trace.get("spans", [])):
        return True
    return False


def collect_reflow_cases(store, limit: int = 200, db=None, with_stats: bool = False):
    """从 TraceStore 拉取问题 Trace 并转成候选评估用例。CLI 与 API 共用。

    db 可选:问题 Trace 与人工回复分别存在 TraceStore(trace 表)与 Database
    (session_archive 表)两处,靠 trace 里的 session_id 关联。传 db 时,若该
    session 归档里有人工坐席回复(HUMAN_AGENT_INTENT),连带抽成
    expected_keywords;不传 db 则完全保持原行为(不查归档,不设关键词)。

    **按用户输入去重。** 改造前不去重,实测一次回流产出 25 条候选里,同一句
    「我买的洗衣机坏了想退货,运费需要我自己出吗」重复了 5 条以上——因为线上
    确实被反复问了很多次。一个回归集里放 5 条一模一样的用例:每次评估都为它
    们各付一次 token、一个偶发行为按 5 倍权重扭曲通过率,而覆盖面一点没增加。

    `with_stats=True` 时返回 `(cases, stats)`,`stats` 说明**丢掉了多少、为什么**。
    默认仍只返回 list:这个函数有两个既有调用方(API 与 CLI),不能改掉默认形状。

    **为什么要报丢弃数**:去掉不可复现的期望之后(见 `trace_to_case`),一部分候选
    会一条断言都不剩,那种用例永远通过、白花 token、还抬高通过率,必须丢。但静默
    丢掉会让人以为"线上就这么点问题"——与本仓库 `service_insufficient`、
    `anomaly_scope`、`degraded` 那批披露同一条纪律:**少给了东西必须说出来。**
    """
    cases = []
    seen: dict[str, int] = {}          # 归一化输入 → 在 cases 里的下标
    dropped_no_assertion = 0
    dropped_inputs: list[str] = []
    for row in store.recent_traces(limit=limit):
        full = store.get_trace(row["trace_id"])
        if not full or not is_problem_trace(full):
            continue
        human_reply = ""
        if db is not None:
            session_id = full.get("session_id")
            if session_id:
                archived = db.get_archived_session(session_id)
                if archived:
                    human_reply = extract_human_reply(archived)
        case = trace_to_case(full, human_reply=human_reply)
        if expectation_suppressed(full) and not case_has_assertions(case):
            dropped_no_assertion += 1
            if len(dropped_inputs) < 20:      # 只留个样本给人看,不把整份日志搬过来
                dropped_inputs.append((full.get("user_input") or "")[:40])
            continue
        key = " ".join((full.get("user_input") or "").split())   # 折叠空白后比对
        if not key:
            cases.append(case)         # 空输入无从去重,原样保留
            continue
        if key not in seen:
            seen[key] = len(cases)
            cases.append(case)
        elif _richer(case, cases[seen[key]]):
            # 同一句话的多条 trace 里保留**信息量最大**的那条:带人工回复关键词、
            # 带期望工具的用例断言更强。先到先得会让一条什么都没断言的空壳
            # 挤掉后面那条真正有价值的。
            cases[seen[key]] = case
    if not with_stats:
        return cases
    return cases, {
        "kept": len(cases),
        "dropped_no_assertion": dropped_no_assertion,
        "dropped_samples": dropped_inputs,
        "note": ("丢弃的是去掉不可复现期望后一条断言都不剩的候选:多数是仅因"
                 "「同一问题重复N次未解决」这种会话级判定而升级的轮次,"
                 "单轮用例复现不了它。留着会永远通过、白花 token 并抬高通过率。"),
    }


def _richer(a: dict, b: dict) -> bool:
    """a 是否比 b 更值得留在回归集里(断言更多 = 更强)。"""
    def score(c: dict) -> int:
        return (len(c.get("expected_keywords") or []) * 2
                + len(c.get("expected_tools") or [])
                + (1 if c.get("expected_intent") else 0)
                + (1 if c.get("expected_requires_human") else 0))
    return score(a) > score(b)


def trace_to_case(trace: dict, human_reply: str = "") -> dict:
    tid = str(trace.get("trace_id", ""))[:8]
    user_input = trace.get("user_input", "")
    case = {
        "id": f"reflow-{tid}",
        "description": f"线上回流: {user_input[:20]}",
        "turns": [user_input],
    }
    intent = trace.get("intent")
    if intent and intent not in ("blocked", "unknown"):
        case["expected_intent"] = intent

    tools = [s["name"].split(":", 1)[1]
             for s in trace.get("spans", [])
             if s.get("kind") == "tool" and ":" in s.get("name", "")]
    if tools:
        case["expected_tools"] = tools

    # **只有本轮可判的升级才能写成单轮用例的期望。**
    #
    # 实测(走查评估页时抓到):一次回流产出 16 条候选,**16 条全是 `turns` 只有一句
    # 的单轮用例,且 16 条全部 `expected_requires_human=True`** ——包括"你们几点上班"
    # "帮我看下订单发货了吗""戴森V15吸尘器 帮我下单这个"这种显然不该转人工的话。
    #
    # 查升级理由:38 次升级里 **27 次(71%)是「同一问题重复3次未解决」** ——那是
    # **会话级**的判定(用户已经问过三遍),而生成的用例只留了那一句话。于是用例断言
    # 的是"看到这一句就该转人工",真实原因却是"这句已经问了三遍"。一个新客服被单独
    # 问一次"你们几点上班"会正确回答,然后**被这条用例判为失败**。
    #
    # 这批用例经 `reflow_traces.py --merge-into app/evaluation/cases.json` 会真写进
    # 回归集,而回归集正是 skill 转正门禁的比较基准(见 docs/交付对照表.md ㊸)。
    # **把不可复现的期望写进裁判尺,比不写用例糟得多。**
    # **只在理由确实记着、且全部是会话级时**才不写这条期望。
    # 没记录理由(老 trace / 别的写入路径)= **判不出**它是不是会话级的,而"判不出"
    # 不等于"它就是会话级的"——那种情况保持既有行为照写,不因为缺一份元数据就把
    # 一整类真实用例悄悄丢掉。这与本轮反复纠正的是同一条:别把"不知道"当成结论。
    hitl_spans = [s for s in trace.get("spans", []) if s.get("kind") == "hitl"]
    hitl_reasons = [r for s in hitl_spans for r in (s.get("meta") or {}).get("reasons", [])]
    if hitl_spans and (not hitl_reasons
                       or any(not is_context_derived_reason(r) for r in hitl_reasons)):
        case["expected_requires_human"] = True

    if human_reply:
        kws = keywords_from_reply(human_reply)
        if kws:
            case["expected_keywords"] = kws

    return case


def extract_human_reply(archived: dict) -> str:
    """取该归档会话里**最后一条**人工坐席回复;没有则空串。

    判据与 golden_corpus 完全同源:坐席回复由 admin_session_reply 写成
    assistant 消息,intent 固定为 human_agent。取最后一条:同一会话里人工可能
    回了多轮,最终那条才是结论。

    `messages` 的实际形状不统一,两个调用方给的不一样:`Database.get_archived_
    session` 返回 `dict(row)`,messages 是**未反序列化的 JSON 字符串**(它没
    有像 `list_recent_archives` 那样 json.loads);而本模块的单元测试/其他调
    用方常常手工构造一个**已解析好的 list**。这里对两种形状都容错,字符串就
    地 `json.loads`,解析失败(脏数据/旧格式)或本来就不是 list 一律当成空
    列表,不让单条异常数据崩掉整条回流链路。

    assistant 内容非 JSON(旧格式纯文本)时跳过,不误判。
    """
    import json as _json
    from app.agent.skills.golden_corpus import HUMAN_AGENT_INTENT

    raw_messages = (archived or {}).get("messages")
    if isinstance(raw_messages, str):
        try:
            raw_messages = _json.loads(raw_messages) if raw_messages else []
        except (ValueError, TypeError):
            raw_messages = []
    if not isinstance(raw_messages, list):
        raw_messages = []

    found = ""
    for msg in raw_messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        try:
            data = _json.loads(str(msg.get("content") or ""))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("intent") == HUMAN_AGENT_INTENT:
            found = str(data.get("reply") or "")
    return found


# 关键词只认「短、有实义、会反复出现」的系统已知词表,不再对人工回复做任意分词:
# - 订单/退款状态标签(与 user_orders.py 展示给用户的措辞完全同源)
# - 商品名(Database.all_products,库不可用时静默跳过,不让取词失败)
# - 承诺/政策类术语(risk.py 的 COMMITMENT_KEYWORDS,资金相关措辞天然简短)
# 命中即用词表原词作为关键词——回复分毫不差地包含该词才算数,不再对回复本身切段,
# 天然规避了「切出一整句长子句当关键词」的问题。
_MIN_KEYWORD_LEN = 2
_MAX_KEYWORD_LEN = 10          # 短语上限:词表里最长的词也远短于此,超限的不可能是本表词


def _reply_vocabulary() -> list[str]:
    """待匹配词表:订单/退款状态 + 商品名 + 承诺类政策术语,按长度降序排列
    (长词先匹配,避免短词抢先占位导致更具体的长词永远输给它的子串)。"""
    from app.agent.tools.user_orders import STATUS_LABELS
    from app.agent.skills.risk import COMMITMENT_KEYWORDS

    terms = set(STATUS_LABELS.values()) | set(COMMITMENT_KEYWORDS)
    try:
        from app.db import get_db
        for p in get_db().all_products():
            name = (p.get("name") or "").strip()
            if name:
                terms.add(name)
    except Exception:
        pass  # 商品库读取失败(如离线/测试环境未初始化)时跳过,不让取词本身报错
    return sorted(terms, key=lambda t: (-len(t), t))


def keywords_from_reply(reply: str, top_n: int = 5) -> list[str]:
    """从人工回复里抽几个内容词,作为评测的 expected_keywords。

    只在系统已有词表(订单/退款状态、商品名、承诺类术语)里找回复里**确实出现**
    的词,而不是对回复本身任意分词——这样抽出来的词天然短小、有实义、能在不同
    表述里重复出现,一次改写措辞不会让期望直接失效,也不会把订单号/日期/金额
    这类只会出现一次的会话特定内容变成永远无法复现的"假期望"。

    抽不出就返回 []——EvalCase 的空期望在评分时**被跳过而不是判 0**
    (见 dataset.py 模块 docstring),所以留空安全,编造才危险。

    刻意用确定性词表匹配而不是 LLM:评测期望必须可复现,且不该每次回流都花钱。

    真正的贪心最长匹配 + 数字剔除 + 双向包含判重逻辑在
    `app.utils.keyword_match.match_known_terms` 里——与
    `app.agent.tools.reviews._bad_terms`(差评关键词)共用同一份实现,这里只
    负责组装"回复用"的词表(见 `_reply_vocabulary`)。
    """
    return match_known_terms(reply, _reply_vocabulary(), top_n=top_n,
                             min_len=_MIN_KEYWORD_LEN, max_len=_MAX_KEYWORD_LEN)
