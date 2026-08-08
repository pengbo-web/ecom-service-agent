"""E2:结构化元数据(intent/confidence/requires_human)零 LLM 派生的回归测试。

背景:旧版 `EcomAgent._extract_structured_response` 在 ReAct 主生成产出回复之后,
还会调用一次 `client.beta.chat.completions.parse` 把同一段文字整段喂给模型,
只为拿 intent/confidence/requires_human 三个字段——它自己的 system prompt 还
写着"reply 字段直接使用原文，不要修改或缩减"，即这次调用完全不产生新回复,
纯粹是买家这一轮里一次多余的 round trip(~5s，约占全轮延迟的 10%)。

本文件锁住两件事:
1. 一轮买家对话(无工具调用的简单轮)只应该有且恰好一次"生成"调用——这是
   本次任务交付的核心属性，任何人再往 chat() 里加回第二次生成都会让它失败。
2. 三个元数据字段改为零 LLM 派生后，取值仍然符合文档里写的语义(QU 意图映射 /
   QU 来源置信度 / 兜底话术关键词命中)，且解析失败时有等效安全网、绝不炸整轮。
"""

import types as _t

from app.agent.chat import EcomAgent
from app.config.settings import settings
from app.schemas.response import IntentType


# ---- OpenAI 响应对象的最小仿造(与 tests/test_react_degrade.py 一致) ----
class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


class _Completions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(kwargs)
        return self.outer.script.pop(0)


class _BetaCompletions:
    """哨兵:谁再把提取逻辑改回调 client.beta.chat.completions.parse，这里直接
    抛异常让测试失败，而不是悄悄真的发一次网络请求。"""

    def __init__(self, outer):
        self.outer = outer

    def parse(self, **kwargs):
        self.outer.beta_calls.append(kwargs)
        raise AssertionError("不应再调用 client.beta.chat.completions.parse")


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta_calls = []
        self.chat = type("Chat", (), {"completions": _Completions(self)})()
        self.beta = type(
            "Beta", (), {"chat": type("Chat", (), {"completions": _BetaCompletions(self)})()}
        )()


class FakeToolManager:
    tool_definitions: list = []

    def __init__(self):
        self.executed = []

    def execute_tool(self, name, args):
        self.executed.append((name, args))
        return '{"success": true}'


def make_agent(script, tm=None, max_steps=3, qu=None):
    """裸 agent(不跑 __init__、不触网),风格与 tests/test_react_degrade.py 一致。"""
    a = EcomAgent.__new__(EcomAgent)
    a.client = FakeClient(script)
    a.model = "test"
    a.temperature = 0.0
    a.max_react_steps = max_steps
    a.tool_manager = tm or FakeToolManager()
    a.raw_messages = []
    a.session_id = None
    a.user_id = None
    a.summary = None
    a.events = []
    a.event_sink = lambda ev: a.events.append(ev)
    a._build_messages = lambda: [{"role": "system", "content": "s"}] + a.raw_messages
    a.session_path = "x.json"
    a.store = _t.SimpleNamespace(save=lambda k, s: None, load=lambda k: None, delete=lambda k: None)
    a._status = "complete"
    a._step_seq = 0
    a._pending = None
    a._turn_qu = qu
    a.skill_manager = None   # 跳过 skill 预加载分支(与本测试无关)
    a.memory_manager = _t.SimpleNamespace(
        stm_to_dict=lambda: {}, update_short_term=lambda msgs, all_messages=None: None,
    )
    from app.agent.reply_pipeline import ReplyPipeline
    a._reply_pipeline = ReplyPipeline()
    return a


# ---------------------------------------------------------------------------
# 1. 核心属性:一轮买家对话恰好一次生成调用
# ---------------------------------------------------------------------------

def test_simple_turn_makes_exactly_one_generation_call():
    """无工具调用的简单轮:主生成 1 次,提取阶段不应再叠加第二次生成。"""
    a = make_agent([
        _Resp(_Msg(content="您好，请问需要什么帮助？", tool_calls=None)),
    ])

    result = a.chat("你好")

    assert len(a.client.calls) == 1     # 唯一一次生成(旧版是 2 次:react + parse)
    assert a.client.beta_calls == []    # 没有第二次结构化提取调用
    assert result.reply == "您好，请问需要什么帮助？"   # 买家看到的文本原样不变,字节级一致


def test_tool_turn_extraction_adds_zero_generation_calls():
    """带一次工具调用的轮次:ReAct 循环自身两步(带工具→收敛)是已有的合法生成,
    锁的是"提取阶段不再叠加一次"——工具轮前后调用数应等于 ReAct 步数,不多一次。"""
    tm = FakeToolManager()
    a = make_agent([
        _Resp(_Msg(content="让我查一下", tool_calls=[_TC("1", "query_order", '{"order_id":"O1"}')])),
        _Resp(_Msg(content="您的订单已发货。", tool_calls=None)),
    ], tm=tm)

    result = a.chat("我的订单到哪了")

    assert len(a.client.calls) == 2     # ReAct 两步自身的生成,未受影响
    assert a.client.beta_calls == []    # 提取阶段零生成
    assert result.reply == "您的订单已发货。"


# ---------------------------------------------------------------------------
# 2. 三个元数据字段的零 LLM 派生语义
# ---------------------------------------------------------------------------

def _qu(intent="其他", domain=None, source="llm"):
    return _t.SimpleNamespace(intent=intent, domain=domain, source=source,
                              need_kb=False, kb_query=None)


def test_intent_reuses_qu_classification_complaint():
    a = make_agent([], qu=_qu(intent="投诉"))
    result = a._extract_structured_response("非常抱歉给您带来不便")
    assert result.intent == IntentType.COMPLAINT


def test_intent_policy_consult_refined_by_domain_aftersale():
    """QU 的"政策咨询"太粗,结合 QU 已判定的 domain 再细分一层。"""
    a = make_agent([], qu=_qu(intent="政策咨询", domain="aftersale"))
    result = a._extract_structured_response("七天无理由退货,联系客服申请即可")
    assert result.intent == IntentType.RETURN_REQUEST


def test_intent_policy_consult_refined_by_domain_presale():
    a = make_agent([], qu=_qu(intent="政策咨询", domain="presale"))
    result = a._extract_structured_response("满 300 减 50")
    assert result.intent == IntentType.PROMOTION


def test_intent_falls_back_to_other_without_qu():
    """引擎独立运行、orchestrator 未注入 QU 时:不猜,落 OTHER。"""
    a = make_agent([], qu=None)
    result = a._extract_structured_response("随便一句话")
    assert result.intent == IntentType.OTHER


def test_confidence_reflects_qu_source_rule_highest():
    a = make_agent([], qu=_qu(source="rule"))
    result = a._extract_structured_response("好的")
    assert result.confidence == a._QU_SOURCE_CONFIDENCE["rule"]


def test_confidence_reflects_qu_source_llm_lower_than_rule():
    a = make_agent([], qu=_qu(source="llm"))
    result = a._extract_structured_response("帮我查下订单")
    assert result.confidence == a._QU_SOURCE_CONFIDENCE["llm"]
    assert a._QU_SOURCE_CONFIDENCE["llm"] < a._QU_SOURCE_CONFIDENCE["rule"]


def test_confidence_low_when_qu_fallback_or_missing():
    """QU 自身兜底(意图来源不明)与未注入 QU 同档,且低于 HITL 默认阈值,
    仍能触发"置信度过低"规则升级——不是形同虚设的常量。"""
    a_fallback = make_agent([], qu=_qu(source="fallback"))
    a_none = make_agent([], qu=None)
    conf_fb = a_fallback._extract_structured_response("x").confidence
    conf_none = a_none._extract_structured_response("x").confidence
    assert conf_fb == conf_none
    assert conf_fb < settings.hitl_confidence_threshold


def test_requires_human_true_when_reply_contains_handoff_phrase():
    """系统提示词规定的统一兜底话术含"转人工/人工客服"字样,命中即置 True——
    信号来源与旧版(读同一段文字判断)一致,只是不再为读它花一次 LLM。"""
    a = make_agent([], qu=_qu())
    result = a._extract_structured_response(
        "这个问题我需要为您核实一下，或为您转接人工客服，好吗？")
    assert result.requires_human is True


def test_requires_human_false_for_ordinary_reply():
    a = make_agent([], qu=_qu())
    result = a._extract_structured_response("您的订单已发货，预计明天送达～")
    assert result.requires_human is False


def test_follow_up_question_is_none_not_fabricated():
    """不再有模型从文本里抽取的追问句;前端 MetadataChips 也未渲染该字段,
    统一 None,与其它非模型分支(blocked/human_request/replay)同口径。"""
    a = make_agent([], qu=_qu())
    result = a._extract_structured_response("随便回复")
    assert result.follow_up_question is None


def test_extract_never_calls_llm():
    """整条提取路径(含 fallback)不应触碰 client——用一个"一调用就报错"的
    client 验证零 LLM。"""
    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError(f"结构化提取不应访问 client.{name}")

    a = make_agent([], qu=_qu(intent="投诉", source="rule"))
    a.client = ExplodingClient()
    result = a._extract_structured_response("回复文本")
    assert result.intent == IntentType.COMPLAINT
    assert result.reply == "回复文本"


# ---------------------------------------------------------------------------
# 3. 等效安全网:_extract_structured_fallback 零 LLM、永不炸整轮
# ---------------------------------------------------------------------------

def test_fallback_is_llm_free_and_safe():
    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError(f"fallback 不应访问 client.{name}")

    a = make_agent([], qu=None)
    a.client = ExplodingClient()
    result = a._extract_structured_fallback("兜底文本")
    assert result.intent == IntentType.OTHER
    assert result.reply == "兜底文本"
    assert 0.0 <= result.confidence <= 1.0
    assert result.follow_up_question is None


def test_extract_falls_back_when_qu_shape_is_malformed():
    """_turn_qu 形状异常(如 intent 不是字符串)不应让整轮报错,落到等效安全网。"""
    class BadQU:
        @property
        def intent(self):
            raise RuntimeError("boom")

    a = make_agent([], qu=BadQU())
    result = a._extract_structured_response("回复文本")
    assert result.intent == IntentType.OTHER   # 走了 fallback,而不是向上抛异常
    assert result.reply == "回复文本"
