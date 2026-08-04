"""确定性技能匹配:按 skill description 里的"适用关键词"匹配用户消息。

为什么需要:实测模型在自然措辞下**从不**主动调 load_skill(catalog 明确要求过、
放在 system 消息末尾、提高 ReAct 步数、往画像 prompt 最前面加强制段,全都无效),
导致工作流守卫(skill 作用域)永不触发、自进化闭环没有输入。
所以加载不能依赖模型自觉 —— 由服务端确定性判定并预加载。

纯逻辑:无 IO、无 LLM、无全局状态,便于单测与复算。
"""

from __future__ import annotations

_KEYWORD_MARKER = "适用关键词："
_SEPARATORS = "、,，/|"


def extract_keywords(description: str) -> list[str]:
    """取 description 里"适用关键词：a、b、c。"之后的关键词;无该段返回 []。"""
    text = description or ""
    idx = text.find(_KEYWORD_MARKER)
    if idx < 0:
        return []
    tail = text[idx + len(_KEYWORD_MARKER):]
    for stop in ("。", "\n"):
        cut = tail.find(stop)
        if cut >= 0:
            tail = tail[:cut]
    words: list[str] = []
    buf = ""
    for ch in tail:
        if ch in _SEPARATORS:
            if buf.strip():
                words.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        words.append(buf.strip())
    return words


def match_skill(user_input: str, catalog: list[dict]) -> str | None:
    """选出最匹配的 skill 名;无命中返回 None。

    确定性排序(不能有随机性:同一句话必须永远选到同一个 skill):
    命中关键词数多者胜 → 平手取最长命中关键词更长者 → 再平手按 name 升序。
    """
    text = user_input or ""
    if not text:
        return None
    scored: list[tuple[int, int, str]] = []
    for entry in catalog or []:
        name = str((entry or {}).get("name") or "")
        if not name:
            continue
        hits = [kw for kw in extract_keywords(str((entry or {}).get("description") or ""))
                if kw and kw in text]
        if hits:
            scored.append((len(hits), max(len(k) for k in hits), name))
    if not scored:
        return None
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return scored[0][2]
