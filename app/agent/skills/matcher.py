"""确定性技能匹配:按 skill description 里的"适用关键词"匹配用户消息。

为什么需要:实测模型在自然措辞下**从不**主动调 load_skill(catalog 明确要求过、
放在 system 消息末尾、提高 ReAct 步数、往画像 prompt 最前面加强制段,全都无效),
导致工作流守卫(skill 作用域)永不触发、自进化闭环没有输入。
所以加载不能依赖模型自觉 —— 由服务端确定性判定并预加载。

纯逻辑:无 IO、无 LLM、无全局状态,便于单测与复算。
"""

from __future__ import annotations

import re

#: 声明关键词的三种写法,实测**全都在用**:
#:
#:     适用关键词：退货、退款、换货          三个买家 skill
#:     关键词：日报、周报、经营情况           三个卖家 skill  ← 只认第一种时全军覆没
#:     关键词包括“优惠券”“有哪些券”          候选 query-coupons
#:
#: **只认"适用关键词："的代价是实测过的。** `daily-business-report` /
#: `product-health-check` / `refund-attribution` 三个卖家 skill 抽出来是 **0 个
#: 关键词** → `match_skill` 永不命中 → 服务端预加载对卖家侧**完全不生效** →
#: 参谋只能靠模型自觉调 `load_skill`,而本模块开头那段话说的就是它不会自觉。
#:
#: 连锁后果有三层,一层比一层隐蔽:
#:   1. skill 的报告模板不生效 —— 实测参谋答退款归因时不按五步模板走;
#:   2. skill 里的硬约束不生效(数据口径必须原样引用、建议要具体到动作);
#:   3. **卖家侧 skill 永远没有执行轨迹** —— 这正是之前查到"四个卖家 skill
#:      门禁用例 0 条"的根因:没有轨迹就没有采样源,自进化对它们完全空转。
#:
#: 前瞻是必需的:把冒号和"包括"都写成可选,散文「没有声明关键词的描述」会被
#: 解析出关键词「的描述」。要求 `关键词` 紧跟冒号或"包括/有/如"之一才算声明。
_KEYWORD_RE = re.compile(
    r"(?:适用)?关键词(?=[:：]|包括|有|如)(?:包括|有|如)?[:：]?\s*(.+)$", re.M)
_SPLIT_RE = re.compile(r"[、,，;；/|]+")
#: 带引号的写法里,引号内才是词,引号外的"包括""等"是散文。
_QUOTED_RE = re.compile(r"[“\"「『]([^”\"」』]{2,12})[”\"」』]")

#: 一个字的"词"(如"退")会命中几乎所有消息,匹配到的 skill 与用户意图无关。
_MIN_KEYWORD_LEN = 2


def extract_keywords(description: str) -> list[str]:
    """取 description 里声明的适用关键词;无声明返回 []。

    三种写法都认(见 `_KEYWORD_RE` 上面的注释)。这是全仓库**唯一**的关键词
    解析实现 —— `case_synthesis.skill_keywords` 也调它。曾经两处各写一份,
    然后各自修各自的,于是同一个 skill 在"预加载"和"合成门禁用例"两条路上
    被解析出不同的关键词。
    """
    text = description or ""
    hit = _KEYWORD_RE.search(text)
    if not hit:
        return []
    tail = hit.group(1)
    for stop in ("。", "\n"):
        cut = tail.find(stop)
        if cut >= 0:
            tail = tail[:cut]
    quoted = _QUOTED_RE.findall(tail)
    words = quoted if quoted else _SPLIT_RE.split(tail)
    out: list[str] = []
    for w in words:
        w = w.strip("“”\"「」『』 ")
        if len(w) >= _MIN_KEYWORD_LEN:
            out.append(w)
    return out


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
