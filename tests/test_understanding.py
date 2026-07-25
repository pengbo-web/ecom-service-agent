"""查询理解节点:规则快筛/LLM JSON 解析/全兜底。"""

from types import SimpleNamespace

from app.agent.understanding import QueryUnderstanding, understand


def _client(reply=None, raises=False, capture=None):
    def create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if raises:
            raise RuntimeError("llm down")
        msg = SimpleNamespace(content=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _hist(*users):
    out = []
    for u in users:
        out.append({"role": "user", "content": u})
        out.append({"role": "assistant", "content": "好的"})
    return out


# ---- 规则快筛(不调 LLM) ----

def test_rule_ack_words_skip_llm():
    c = _client(raises=True)                      # 若真调 LLM 会抛
    for text in ["嗯", "好的", "OK", "收到", "明白了"]:
        qu = understand(text, [], c, "m")
        assert qu.need_kb is False and qu.source == "rule"
        assert qu.intent == "闲聊寒暄" and qu.domain is None


def test_rule_pure_order_id():
    qu = understand("ORD-20240115-001", [], _client(raises=True), "m")
    assert qu.need_kb is False and qu.intent == "订单事务" and qu.source == "rule"
    assert qu.domain == "midsale"    # 纯单号定向售中,防粘在缺订单工具的 presale


def test_rule_human_handoff():
    qu = understand("转人工", [], _client(raises=True), "m")
    assert qu.need_kb is False and qu.intent == "转人工" and qu.source == "rule"


def test_rule_only_for_short_text():
    """超 20 字即使含语气词也交给 LLM(此处 LLM 失败走兜底,证明没走规则层)。"""
    long_text = "好的好的,那我想再问一下退货的运费到底是谁来承担这个费用呢"
    qu = understand(long_text, [], _client(raises=True), "m")
    assert qu.source == "fallback" and qu.need_kb is True


# ---- LLM 路径 ----

def test_llm_full_parse(monkeypatch):
    cap = {}
    c = _client(reply='{"domain": "aftersale", "intent": "政策咨询", "need_kb": true, "kb_query": "退货运费谁承担"}',
                capture=cap)
    qu = understand("那运费呢?", _hist("退货政策是什么"), c, "m")
    assert qu == QueryUnderstanding(domain="aftersale", intent="政策咨询",
                                    need_kb=True, kb_query="退货运费谁承担", source="llm")
    sent = str(cap["messages"])
    assert "退货政策是什么" in sent and "那运费呢?" in sent   # 历史与原句都送给了 LLM


def test_llm_code_fence_tolerated():
    c = _client(reply='```json\n{"domain": "presale", "intent": "商品咨询", "need_kb": false, "kb_query": null}\n```')
    qu = understand("这双鞋有42码吗", [], c, "m")
    assert qu.domain == "presale" and qu.need_kb is False and qu.kb_query is None


def test_llm_invalid_domain_becomes_none():
    c = _client(reply='{"domain": "unknown", "intent": "其他", "need_kb": true, "kb_query": "x"}')
    qu = understand("随便问问", [], c, "m")
    assert qu.domain is None and qu.source == "llm"     # 非法 domain 置 None,粘性路由接管


def test_llm_need_kb_without_query_uses_raw():
    c = _client(reply='{"domain": "aftersale", "intent": "政策咨询", "need_kb": true, "kb_query": null}')
    qu = understand("价保多久", [], c, "m")
    assert qu.kb_query == "价保多久"                     # need_kb 但没给查询 → 原句兜底


# ---- 兜底 ----

def test_llm_error_falls_back_open():
    qu = understand("退货运费谁出", [], _client(raises=True), "m")
    assert qu.need_kb is True and qu.kb_query == "退货运费谁出"
    assert qu.domain is None and qu.source == "fallback"


def test_bad_json_falls_back_open():
    qu = understand("退货运费谁出", [], _client(reply="我觉得应该检索"), "m")
    assert qu.need_kb is True and qu.source == "fallback"
