"""线上 Trace 回流成评估用例（问题对话沉淀为回归测试）。"""

from app.utils.keyword_match import match_known_terms


def is_problem_trace(trace: dict) -> bool:
    if trace.get("status") == "error":
        return True
    if trace.get("intent") == "blocked":
        return True
    if any(s.get("kind") == "hitl" for s in trace.get("spans", [])):
        return True
    return False


def collect_reflow_cases(store, limit: int = 200, db=None) -> list:
    """从 TraceStore 拉取问题 Trace 并转成候选评估用例。CLI 与 API 共用。

    db 可选:问题 Trace 与人工回复分别存在 TraceStore(trace 表)与 Database
    (session_archive 表)两处,靠 trace 里的 session_id 关联。传 db 时,若该
    session 归档里有人工坐席回复(HUMAN_AGENT_INTENT),连带抽成
    expected_keywords;不传 db 则完全保持原行为(不查归档,不设关键词)。
    """
    cases = []
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
        cases.append(trace_to_case(full, human_reply=human_reply))
    return cases


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

    if any(s.get("kind") == "hitl" for s in trace.get("spans", [])):
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
