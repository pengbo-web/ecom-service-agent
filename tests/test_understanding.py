"""查询理解节点:规则快筛/LLM JSON 解析/全兜底。"""

from types import SimpleNamespace

from app.agent.understanding import QueryUnderstanding, understand
from app.config.settings import settings


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


# ---- L3②:扩展规则表(settings.qu_fast_path_extended_enabled) ----

def test_extended_order_query_skips_llm(monkeypatch):
    monkeypatch.setattr(settings, "qu_fast_path_extended_enabled", True)
    c = _client(raises=True)   # 若真调 LLM 会抛
    for text in ["查一下我的订单", "查订单", "我有哪些订单",
                 "帮我查一下我名下的订单都有哪些", "我的订单有哪些？"]:
        qu = understand(text, [], c, "m")
        assert qu.need_kb is False and qu.source == "rule"
        assert qu.intent == "订单事务" and qu.domain is None   # list_user_orders 三域都有,不强制域


def test_extended_bargain_forces_presale(monkeypatch):
    monkeypatch.setattr(settings, "qu_fast_path_extended_enabled", True)
    qu = understand("这个能便宜点吗，帮我砍砍价", [], _client(raises=True), "m")
    assert qu.need_kb is False and qu.source == "rule"
    assert qu.intent == "商品咨询" and qu.domain == "presale"   # negotiate_price 专属工具,强制域


def test_extended_product_info_skips_llm(monkeypatch):
    monkeypatch.setattr(settings, "qu_fast_path_extended_enabled", True)
    c = _client(raises=True)
    for text in ["这是什么", "多少钱", "这个商品有哪些规格和属性？", "你有什么商品"]:
        qu = understand(text, [], c, "m")
        assert qu.need_kb is False and qu.source == "rule" and qu.intent == "商品咨询"
        assert qu.domain is None


def test_extended_policy_faq_skips_llm_but_still_needs_kb(monkeypatch):
    """省的是 QU 那次 LLM 调用,不省检索——need_kb 仍 True,kb_query=原句。"""
    monkeypatch.setattr(settings, "qu_fast_path_extended_enabled", True)
    c = _client(raises=True)
    for text in ["退货政策是什么", "怎么退货", "价保多久",
                 "退款怎么还没到账", "大件家电退货运费怎么算"]:
        qu = understand(text, [], c, "m")
        assert qu.source == "rule" and qu.intent == "政策咨询"
        assert qu.need_kb is True and qu.kb_query == text
        assert qu.domain is None   # 同一句可能发生在售前或售后,不强制域(避免分错域丢工具)


def test_extended_rules_do_not_misroute_ambiguous_phrasings(monkeypatch):
    """宁漏勿错杀的另一面:这些句子故意不收进扩展表,必须落到 LLM/兜底,
    不能被误判成零检索或错误 domain——验证的是"没有误命中",不是"LLM 判对"。"""
    monkeypatch.setattr(settings, "qu_fast_path_extended_enabled", True)
    ambiguous = [
        "那运费呢?",                     # 需要上文指代消解,规则拿不到上下文
        "我要退货,一步步教我怎么操作",      # 政策问答 vs 发起退货流程双关
        "查一下我的订单为什么被取消了",      # 附加从句,不是纯粹的"列订单"
        "AirPods多少钱",                  # 带具体商品名,不是裸"多少钱"
        "这个多少钱啊而且能便宜点吗",        # 复合句,不是单一议价请求
    ]
    for text in ambiguous:
        c = _client(reply='{"domain": "aftersale", "intent": "其他", "need_kb": true, "kb_query": "x"}')
        qu = understand(text, [], c, "m")
        assert qu.source != "rule", f"{text!r} 不应被规则命中"


def test_extended_rules_switch_off_reverts_to_llm(monkeypatch):
    """关开关=只保留基线 4 条,扩展规则一条都不生效,逐字节回退老行为。"""
    monkeypatch.setattr(settings, "qu_fast_path_extended_enabled", False)
    c = _client(reply='{"domain": "midsale", "intent": "订单事务", "need_kb": false, "kb_query": null}')
    qu = understand("查一下我的订单", [], c, "m")
    assert qu.source == "llm"   # 没有走规则(关开关后这句话落到了 LLM 路径)


def test_baseline_rules_unaffected_by_extended_switch(monkeypatch):
    """基线 4 条不受扩展开关影响,关了扩展依旧走规则。"""
    monkeypatch.setattr(settings, "qu_fast_path_extended_enabled", False)
    qu = understand("你好", [], _client(raises=True), "m")
    assert qu.need_kb is False and qu.source == "rule" and qu.intent == "闲聊寒暄"
