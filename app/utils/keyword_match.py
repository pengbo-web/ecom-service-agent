"""词表匹配:从候选词表里抽取文本中确实出现的词——贪心长词优先、
双向包含判重、含数字的词一律剔除。

抽取算法本身只在这里存一份。它此前在 `app/evaluation/trace_to_case.py`
(评测期望关键词)与 `app/agent/tools/reviews.py`(差评关键词)里各有一份
近乎逐行相同的拷贝:同一套词表组装思路、同一条数字剔除规则、同一种贪心
最长匹配 + 子串判重。这仓库已经因为"手抄表悄悄漂移"栽过三次(前端状态
标签映射、候选校验器的已知工具清单、控制台的商机类型表)——复制一段
*算法*是同一类失败的延时引信:在一处修好一个匹配 bug,另一处照样带着它,
不会报错,只会安静地给出不一致的结果。

两个调用方各自决定"词表是什么"(评测用回复词表、差评分析用差评词表)——
词表来源不下沉到这里,那是两边合法的差异点,不该被这份共享实现拉扯成
同一份词表。
"""

from __future__ import annotations

import re

_DIGIT_RE = re.compile(r"\d")


def match_known_terms(text: str, vocabulary: list[str], top_n: int = 5,
                      min_len: int = 2, max_len: int = 10) -> list[str]:
    """在 `text` 里找 `vocabulary` 中确实出现的词,最多取 `top_n` 个。

    `vocabulary` 应已按长度降序排列(长词优先,避免短词抢先占位导致更具体的
    长词永远输给它的子串)。长度落在 [min_len, max_len] 之外的词跳过;含数字
    的词(订单号/日期/金额/型号类)一律剔除,因为不可能在下一次文本里原样
    复现;已收录词与新词双向包含时判重,不让长短词并存。

    抽不出就返回 []——比编造安全,调用方据此把"没有可靠期望"和"确实有
    期望"分开处理。
    """
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = []
    for term in vocabulary:
        if not term or not (min_len <= len(term) <= max_len):
            continue
        if _DIGIT_RE.search(term):
            continue  # 含数字:订单号/日期/金额/型号类,不可能原样复现
        if term not in text:
            continue
        if any(term in o or o in term for o in out):  # 双向包含判重
            continue
        out.append(term)
        if len(out) >= max(1, int(top_n)):
            break
    return out
