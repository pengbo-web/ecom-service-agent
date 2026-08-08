"""L3② 实测发现的真实缺口:闲聊寒暄类问候/感谢/告别命中规则快筛后,查询理解
本身零 LLM,但生成回复原来仍要走一次完整 ReAct/生成调用——复用
app/hardening/fast_path.py 的判定与文案,在引擎层再兜一次,做到真正零 LLM。
"""

from app.agent.chat import EcomAgent
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings


def _agent(tmp_path, monkeypatch):
    agent = EcomAgent(session_path=str(tmp_path / "s.json"))
    monkeypatch.setattr(agent, "_write_snapshot", lambda: None)   # 隔离:不落真实快照库
    # 若真的走到 react_loop 说明短路没生效——直接炸掉,比"侧面观察耗时"更可靠。
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: (_ for _ in ()).throw(AssertionError("不该调用 LLM 生成")))
    return agent


def test_greeting_short_circuits_with_zero_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "fast_path_enabled", True)
    agent = _agent(tmp_path, monkeypatch)
    agent.set_turn_understanding(QueryUnderstanding(
        domain=None, intent="闲聊寒暄", need_kb=False, source="rule"))
    result = agent.chat("你好")
    assert result.reply == "您好，我是并夕夕智能客服小夕 😊 请问需要查订单、看物流、咨询商品还是售后呢？"
    assert result.requires_human is False
    # 会话历史照常落账:user + assistant 两条(与 FAQ 秒答分支同姿态)
    assert agent.raw_messages[-2] == {"role": "user", "content": "你好"}
    assert agent.raw_messages[-1]["role"] == "assistant"


def test_thanks_and_bye_also_short_circuit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "fast_path_enabled", True)
    for text in ["谢谢", "拜拜"]:
        agent = _agent(tmp_path, monkeypatch)
        agent.set_turn_understanding(QueryUnderstanding(
            domain=None, intent="闲聊寒暄", need_kb=False, source="rule"))
        result = agent.chat(text)
        assert result.reply   # 有确定的文案,不是空的


def test_ack_words_without_a_safe_canned_reply_still_generate(tmp_path, monkeypatch):
    """"好的"/"嗯"这类确认语气词命中规则表(闲聊寒暄+source=rule),但
    fast_path.py 没有为它们收录安全文案——不强行套用,继续走生成,不冒险
    用一句可能答非所问的固定话术糊弄买家。"""
    monkeypatch.setattr(settings, "fast_path_enabled", True)
    agent = EcomAgent(session_path=str(tmp_path / "s2.json"))
    monkeypatch.setattr(agent, "_write_snapshot", lambda: None)
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: '{"intent":"other","confidence":0.9,"reply":"好的,还有什么可以帮您","requires_human":false}')
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())
    agent.set_turn_understanding(QueryUnderstanding(
        domain=None, intent="闲聊寒暄", need_kb=False, source="rule"))
    result = agent.chat("好的")
    assert "好的,还有什么可以帮您" in result.reply   # 走的是 react_loop 桩,证明确实生成了


def test_llm_classified_greeting_does_not_short_circuit(tmp_path, monkeypatch):
    """source="llm"(不是规则命中)一律不走这条零 LLM 短路——短路的前提是
    "规则已经用同一份正则精确锚定过这句话",LLM 自己判的"闲聊寒暄"没有这层
    保证,继续走原有生成流程。"""
    monkeypatch.setattr(settings, "fast_path_enabled", True)
    agent = EcomAgent(session_path=str(tmp_path / "s3.json"))
    monkeypatch.setattr(agent, "_write_snapshot", lambda: None)
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: '{"intent":"other","confidence":0.9,"reply":"正常生成的回复","requires_human":false}')
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())
    agent.set_turn_understanding(QueryUnderstanding(
        domain=None, intent="闲聊寒暄", need_kb=False, source="llm"))
    result = agent.chat("你好呀,今天心情不错")
    assert "正常生成的回复" in result.reply


def test_switch_off_disables_the_shortcut(tmp_path, monkeypatch):
    """关 fast_path_enabled:回退到原有行为,一律走生成——用与
    test_ack_words_without_a_safe_canned_reply_still_generate 相同的桩,
    验证"你好"此时也走生成而不是短路。"""
    monkeypatch.setattr(settings, "fast_path_enabled", False)
    agent = EcomAgent(session_path=str(tmp_path / "s4.json"))
    monkeypatch.setattr(agent, "_write_snapshot", lambda: None)
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: '{"intent":"other","confidence":0.9,"reply":"生成的问候回复","requires_human":false}')
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())
    agent.set_turn_understanding(QueryUnderstanding(
        domain=None, intent="闲聊寒暄", need_kb=False, source="rule"))
    result = agent.chat("你好")
    assert "生成的问候回复" in result.reply
