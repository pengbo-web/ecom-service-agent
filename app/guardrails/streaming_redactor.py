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

屏障字符(barrier)——为什么固定扣 ``holdback`` 个字符太保守:
  上面那套论证给出的是**与文本内容无关**的上界。代价实测出来是致命的:本仓库
  最宽的模式是邮箱(W=114)，于是**长度不足 114 字的回复一条 delta 都发不出
  去**(实测 75 字的回复流式条数为 0，买家干等到最后一次性看到全文)，流式在
  体验上等于没做。

  但内容是能用的信息:邮箱那条正则只吃 ``[\\w.+-]`` 和 ``@``，空格、``*``、
  ``：``、``，`` 这些字符**不可能出现在它的任何一次匹配里**(见
  ``pattern_width.pattern_alphabet``)——而中文客服回复里这类字符每几个字就有
  一个。既然匹配跨不过屏障字符，那么"起点在最后一个屏障之前的匹配"必然在那个
  屏障之前就结束了，其全部字符都已在缓冲区里 ⇒ 已完全确定，不必再等。

  于是每条模式 p 各有自己的已解决边界(取两者较宽的那个)::

      resolved_p = max(最后一个屏障字符的下标 + 1, len(buffer) - W_p)

  全局边界取各模式的**最小值**(任一条模式还没定，就不能提交)。字母表算不出来
  的模式(含 ``.``、``[^...]`` 等)退回 ``len - W_p``，与改造前逐字节一致。

  这是**收窄等待，不是放松检测**:正则一个字没改，判定结果不变，只是不再为
  逻辑上不可能存在的匹配白等。屏障只影响"何时敢提交"，"提交的内容里有没有
  匹配"这个结论仍由 ``finditer`` 在完整缓冲区上给出。

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

from app.guardrails.pattern_width import (
    UnboundedPatternError, pattern_alphabet, pattern_max_width,
)


class IncrementalRedactor:
    """单个"这一轮"专用，不可跨轮复用——`app/api/streaming.py` 每轮新建一个。"""

    def __init__(self, patterns: list, holdback: int, sanitize_fn):
        self._patterns = list(patterns)
        self._holdback = max(int(holdback), 0)
        self._sanitize_fn = sanitize_fn   # (原始片段文本) -> 脱敏后的片段文本
        self._buffer = ""       # 本轮至今收到的全部原始文本(从未截断)
        self._committed = 0     # 已经吐给买家的原始字符数(游标)
        # 每条模式的 (最长宽度, 字母表谓词或 None, 字母表判定缓存, 最后屏障下标)。
        # 宽度算不出来的模式(直接构造 IncrementalRedactor 的测试可能塞任意正则)
        # 退回构造参数里那个全局 holdback，保守但安全。
        self._pstate: list = []
        for p in self._patterns:
            try:
                width = pattern_max_width(p)
            except UnboundedPatternError:
                width = self._holdback
            self._pstate.append({
                "width": max(int(width), 0),
                "alpha": pattern_alphabet(p),
                "cache": {},
                "barrier": -1,   # 已扫描范围内最后一个屏障字符的下标
                "scanned": 0,    # 屏障扫描进度(缓冲区只增长，扫过的不必重扫)
            })

    def _resolved_boundary(self) -> int:
        """各模式已解决边界的最小值——起点小于它的匹配全都不会再变。"""
        n = len(self._buffer)
        if not self._pstate:
            return n            # 没有模式要防:全部可提交
        best = n
        for st in self._pstate:
            alpha = st["alpha"]
            if alpha is not None:
                # 只扫新到达的那一段:屏障下标随缓冲区增长单调不减
                cache = st["cache"]
                for i in range(st["scanned"], n):
                    ch = self._buffer[i]
                    ok = cache.get(ch)
                    if ok is None:
                        ok = alpha(ch)
                        cache[ch] = ok
                    if not ok:
                        st["barrier"] = i
                st["scanned"] = n
                resolved_p = max(st["barrier"] + 1, n - st["width"])
            else:
                resolved_p = n - st["width"]
            if resolved_p < best:
                best = resolved_p
        return max(0, best)

    def feed(self, content: str) -> Optional[str]:
        """喂入新到达的一小段原始文本。返回这一步"确认安全、可以吐给买家"
        的脱敏后文本;返回 ``None`` 表示这次没有新增的可提交内容(还在
        holdback 区间内,继续攒缓冲区，不发任何东西)。"""
        self._buffer += content or ""
        resolved = self._resolved_boundary()
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
