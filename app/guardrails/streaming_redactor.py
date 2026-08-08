"""E1b(Part1):把"局部脱敏类"输出护栏安全地套到逐块流式的 reply_delta 上。

标准增量正则脱敏手法:维护一个尚未提交(可能还会被后续到达的字符影响判定
结果)的原始文本缓冲区,只把"未来任何新到达的字符都不可能再改变判定结果"
的前缀吐给买家,尾部至少保留 ``holdback`` 个字符不吐——``holdback`` 由
``GuardPipeline.local_redaction_holdback()`` 从护栏自己的正则模式静态推
导（见 ``pattern_width.py``），不是这里写死的数字。

正确性论证(为什么"扣住尾部 holdback 个字符"就足够安全):
  设某条护栏正则的最长可能匹配宽度为 W（``holdback = max(所有护栏的 W)``）。
  一个从位置 s 开始的匹配，只会消费最多 W 个字符（宽度上限是所有护栏正则
  的公共上界），也就是说，只要缓冲区里 s 之后已经有 >= W 个字符，这个匹配
  "有没有命中、命中的话具体是哪一段"这件事就已经完全确定，之后再往缓冲区
  追加多少字符都不会改变这个结论——所以只要 ``len(buffer) - s >= holdback``
  （即 s 不落在最后 holdback 个字符里），位置 s 就是"已解决"（resolved）的。

  据此，任何起点 < ``resolved = len(buffer) - holdback`` 的匹配都已经完全
  确定（起点、终点都不会再变）。用 ``pattern.finditer(buffer)`` 在**完整**
  缓冲区（不截断！截断会让跨边界的匹配因为缺字符而"看起来没匹配"，反而漏
  脱敏）上找出所有这类已解决的匹配，如果其中某个匹配的终点越过了
  ``resolved`` 边界（起点早、但匹配本身较长，尾部伸到了 holdback 区间
  里），就把可提交边界顺势推到这个匹配的终点——这段内容本来就已经在本地
  缓冲区里，只是还没"发货"，没有必要为了凑一个更简单的边界公式而多等一轮。

  已解决匹配的收尾边界之外、到 ``resolved`` 为止的普通文本，同理"没有匹配
  从这里开始"这件事也已经解决（``finditer`` 在完整缓冲区上找不到，就是找
  不到）。

尾部零宽断言(如 ``(?!\\d)``)在缓冲区末尾提前判定为"未来不会有数字"的唯一
风险方向是**多脱敏**（把后来证明并不敏感的内容也顺手脱了）——安全侧的
误报，不是漏报，不违反"不泄漏"这条硬约束，因此不需要额外加宽 holdback。

生成结束时，缓冲区里剩下没提交的尾部**直接丢弃**（不再单独发一条
delta）——本模块只负责"提前吐一部分安全的字给买家看"这件锻炼耐心的事，
完整、权威的最终文本仍然由 ``GuardPipeline.check_output`` 在整段回复上跑
一次生成最终的 ``reply`` 事件（前端以终帧为准，见 app/api/streaming.py
`_finalize`），本模块提交与否都不影响那次检查的正确性。
"""

from typing import Optional


class IncrementalRedactor:
    """单个"这一轮"专用，不可跨轮复用——`app/api/streaming.py` 每轮新建一个。"""

    def __init__(self, patterns: list, holdback: int, sanitize_fn):
        self._patterns = list(patterns)
        self._holdback = max(int(holdback), 0)
        self._sanitize_fn = sanitize_fn   # (原始片段文本) -> 脱敏后的片段文本
        self._buffer = ""       # 本轮至今收到的全部原始文本(从未截断)
        self._committed = 0     # 已经吐给买家的原始字符数(游标)

    def feed(self, content: str) -> Optional[str]:
        """喂入新到达的一小段原始文本。返回这一步"确认安全、可以吐给买家"
        的脱敏后文本;返回 ``None`` 表示这次没有新增的可提交内容(还在
        holdback 区间内,继续攒缓冲区，不发任何东西)。"""
        self._buffer += content or ""
        resolved = max(0, len(self._buffer) - self._holdback)
        boundary = resolved
        for pat in self._patterns:
            for m in pat.finditer(self._buffer):
                if m.start() < resolved and m.end() > boundary:
                    boundary = m.end()   # 起点已解决、但匹配本身伸到了 resolved 之后
        if boundary <= self._committed:
            return None
        raw_new = self._buffer[self._committed:boundary]
        self._committed = boundary
        return self._sanitize_fn(raw_new)
