"""线上 Trace 回流成评估用例（问题对话沉淀为回归测试）。"""


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

    assistant 内容非 JSON(旧格式纯文本)时跳过,不误判。
    """
    import json as _json
    from app.agent.skills.golden_corpus import HUMAN_AGENT_INTENT

    found = ""
    for msg in archived.get("messages") or []:
        if msg.get("role") != "assistant":
            continue
        try:
            data = _json.loads(str(msg.get("content") or ""))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("intent") == HUMAN_AGENT_INTENT:
            found = str(data.get("reply") or "")
    return found


# 中文停用词/客套词:出现在几乎每条回复里,当期望关键词等于不设期望
_STOPWORDS = {
    "的", "了", "我", "您", "你", "是", "在", "有", "和", "就", "都", "会",
    "请", "好的", "麻烦", "稍等", "感谢", "抱歉", "不好意思", "亲",
}
_MIN_KEYWORD_LEN = 2


def keywords_from_reply(reply: str, top_n: int = 5) -> list[str]:
    """从人工回复里抽几个内容词,作为评测的 expected_keywords。

    抽不出就返回 []——EvalCase 的空期望在评分时**被跳过而不是判 0**
    (见 dataset.py 模块 docstring),所以留空安全,编造才危险。

    刻意用确定性切分而不是 LLM:评测期望必须可复现,且不该每次回流都花钱。
    """
    import re

    text = (reply or "").strip()
    if not text:
        return []
    # 按标点与空白切成短语,再滤掉停用词与过短片段
    parts = [p.strip() for p in re.split(r"[，。,.;；:：!！?？\s~、]+", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if len(p) < _MIN_KEYWORD_LEN or p in _STOPWORDS:
            continue
        if any(p == o or p in o for o in out):
            continue
        out.append(p)
        if len(out) >= max(1, int(top_n)):
            break
    return out
