"""上下文防线工具（借鉴 nanobot agent/context_governance.py）。

均为纯函数,作用在"送模型的消息副本"上,不改持久化的 raw_messages:
- estimate_tokens      : 粗估消息序列 token 数(用于按预算触发压缩)
- truncate_tool_result : 超大工具结果截断,防单条结果撑爆窗口
- sanitize_tool_pairs  : 修复 tool_calls/tool 结果不成对的非法态(防模型侧 400)
- legal_message_start  : 找一个"从 user 回合开始"的合法起点
"""

from typing import Optional

_TRUNCATE_NOTE = "\n…[结果过长已截断,如需完整信息请缩小查询范围]"


def estimate_tokens(messages: list[dict]) -> int:
    """粗略估算 token 数(中文约 1.5 字/token,英文约 4 字/token,统一按 2 折中)+ 每条约 4 token 开销。"""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            total += len(str(fn.get("name", ""))) + len(str(fn.get("arguments", "")))
        total += 8  # 角色/结构开销
    return total // 2 + 4 * len(messages)


def truncate_tool_result(content: str, limit: int) -> str:
    """工具结果超过 limit 字符则截断并加提示。"""
    if not isinstance(content, str) or len(content) <= limit:
        return content
    return content[:limit] + _TRUNCATE_NOTE


def _valid_tool_call(tc: dict) -> bool:
    fn = tc.get("function") if isinstance(tc, dict) else None
    name = fn.get("name") if isinstance(fn, dict) else None
    return isinstance(name, str) and bool(name.strip())


def unwrap_assistant_envelope(messages: list[dict]) -> list[dict]:
    """把历史里 assistant 消息的结构化信封还原成"买家实际看到的那句话"。

    **这是一个实测到的缺陷的修复。** 买家发「知道了」,收到的回复是:

        好的，感谢您的理解与支持。如有其他问题，随时欢迎联系我。

        {"intent": "acknowledgement", "confidence": 1.0, "reply": "好的，感谢…",
         "requires_human": false, "follow_up_question": null}

    内部字段(置信度、是否转人工)直接摆到了买家眼前,正文还重复了两遍。

    根因不在护栏,也不在解析——`chat.py` 那句
    `raw_messages.append({"role": "assistant", "content": result.model_dump_json()})`
    把**整个结构化响应的 JSON dump** 写进了对话历史。于是历史里每一轮 assistant
    都长成 JSON,模型下一轮**照着历史的格式模仿**。实测口径:全部会话 221 条
    assistant 消息里 100 条(45%)是 JSON 信封——而且是**混着**纯文本的,这恰恰
    是格式模仿最容易出错的情形(模型两种都见过,于是有时挑错那一种)。

    **为什么修在这里,而不是改持久化格式**:那个 JSON 是**承重**的,有四个消费方
    在读它并 `json.loads`——`app/api/history.py`(前端历史回显)、
    `skills/golden_corpus.py`、`skills/user_modeling.py`、`evaluation/trace_to_case.py`。
    把持久化改成纯文本会同时打断这四处。而本模块的既定职责正好是"作用在送模型的
    消息副本上,不改持久化的 raw_messages"(见模块 docstring),所以在这条边界上
    解包:落库的还是信封,**模型只看得到买家看到的那句话**。

    保守到什么程度:只在 content 能解析成 dict、且 `reply` 是字符串时替换。带
    tool_calls 的 assistant 消息(content 为空)、纯文本回复、解析不出来的畸形
    内容一律原样保留——宁可漏解包一条(退回改造前的行为),也不能把一条正常
    消息弄坏。
    """
    import json as _json

    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "assistant" or not isinstance(content, str):
            out.append(m)
            continue
        s = content.lstrip()
        if not s.startswith("{"):
            out.append(m)          # 绝大多数消息走这条:一次字符判断,不做 JSON 解析
            continue
        try:
            data = _json.loads(s)
        except Exception:          # noqa: BLE001 畸形内容原样保留,不是这里该管的事
            out.append(m)
            continue
        reply = data.get("reply") if isinstance(data, dict) else None
        if not isinstance(reply, str):
            out.append(m)
            continue
        m2 = dict(m)               # 副本:绝不就地改调用方的 raw_messages
        m2["content"] = reply
        out.append(m2)
    return out


def sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
    """保证 assistant.tool_calls 与 tool 结果成对合法。顺序:剥离畸形 → 丢弃悬空 → 回填缺失。"""
    msgs = _strip_malformed_tool_calls(messages)
    msgs = _drop_orphan_tool_results(msgs)
    msgs = _backfill_missing_tool_results(msgs)
    return msgs


def _strip_malformed_tool_calls(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            out.append(m)
            continue
        kept = [tc for tc in m["tool_calls"] if _valid_tool_call(tc)]
        if len(kept) == len(m["tool_calls"]):
            out.append(m)
            continue
        repaired = dict(m)
        if kept:
            repaired["tool_calls"] = kept
        else:
            repaired.pop("tool_calls", None)
        # assistant 既无内容又无有效工具调用 → 整条丢弃
        if not kept and not (repaired.get("content") or "").strip():
            continue
        out.append(repaired)
    return out


def _drop_orphan_tool_results(messages: list[dict]) -> list[dict]:
    declared: set[str] = set()
    out = []
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        if m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not tid or str(tid) not in declared:
                continue  # 悬空 tool 结果,丢弃
        out.append(m)
    return out


def _backfill_missing_tool_results(messages: list[dict]) -> list[dict]:
    declared: list[tuple[int, str]] = []
    fulfilled: set[str] = set()
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.append((i, str(tc["id"])))
        elif m.get("role") == "tool" and m.get("tool_call_id"):
            fulfilled.add(str(m["tool_call_id"]))

    missing = [(i, cid) for i, cid in declared if cid not in fulfilled]
    if not missing:
        return messages

    out = list(messages)
    offset = 0
    for assistant_idx, call_id in missing:
        insert_at = assistant_idx + 1 + offset
        while insert_at < len(out) and out[insert_at].get("role") == "tool":
            insert_at += 1
        out.insert(insert_at, {
            "role": "tool", "tool_call_id": call_id,
            "content": "[该工具结果缺失,已回填占位]",
        })
        offset += 1
    return out


def legal_message_start(messages: list[dict]) -> int:
    """返回第一个 user 消息的下标(从这里开始回放最安全);无 user 则返回 0。"""
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            return i
    return 0
