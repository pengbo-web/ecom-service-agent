"""转人工快匹配漏一个词的代价:多一次编造承诺的机会。

**实测缺陷。** 发「我要找人工客服」没有命中 `match_human_fast`,于是绕过了
`streaming.py::_human_request_flow` 那句**审过的固定话术**——

    "好的,正在为您转接人工客服,请稍候~ 转接期间您可以继续补充问题,
     人工客服会看到完整对话记录。"

落到模型生成后,模型回的是:

    「好的,我这就为您转接人工客服。请稍候,**系统将在30秒内为您接入**专属服务人员。」

凭空许了一个 SLA,而 hitl 库里当时积压着 42 条待接管。也就是说:**这个匹配器每漏
一种说法,就多一次编造承诺的机会**——漏的不是一次短路优化,是一道话术护栏。

尤其刺眼的是 `转接人工` 之前也漏,而那正是系统自己那句固定话术里的用词
(「正在为您转接人工客服」)——买家照着系统的说法讲,反而匹配不上。
"""

import pytest

from app.hitl.escalation import match_human_fast


@pytest.mark.parametrize("text", [
    # 原本就命中的,回归保护
    "转人工", "人工客服", "找人工", "我要人工", "人工服务", "真人客服",
    # 实测漏掉的
    "我要找人工客服",
    "转接人工",          # 系统自己那句固定话术里的用词
    "要真人", "找真人", "我想转人工", "叫人工", "我要转接人工", "人工",
    # 带标点
    "转人工！", "我要找人工客服~", "转接人工。",
])
def test_pure_handoff_requests_match(text):
    assert match_human_fast(text) is True, f"漏掉「{text}」= 多一次编造承诺的机会"


@pytest.mark.parametrize("text", [
    # 这些是**正常问句**,不是转人工请求。误伤它们会让买家问个问题就被丢进人工队列。
    "人工审核要多久",
    "人工客服几点上班",
    "转人工要等多久吗",
    "人工客服的服务时间",
    "这个能人工处理吗",
    "我的订单呢",
    "",
])
def test_normal_questions_do_not_match(text):
    assert match_human_fast(text) is False, f"误伤「{text}」"


def test_长句不走快路径():
    """≤10 字上限保留:带上下文的长句不是"纯请求",交给正常流程判定(它那边有
    LLM 意图 + 关键词 + 情绪三路兜底,见 should_escalate)。"""
    assert match_human_fast("这个问题你解决不了，我要找人工客服") is False


def test_canned_handoff_reply_has_no_time_promise():
    """固定话术本身不许含时效承诺——它是这条路上唯一不经模型的出话。"""
    import inspect
    import app.api.streaming as st
    src = inspect.getsource(st)
    idx = src.find("正在为您转接人工客服")
    assert idx > 0, "固定话术不见了,这条断言需要跟着改"
    line = src[idx - 100:idx + 200]
    for bad in ("30秒", "秒内", "分钟内", "小时内", "立刻接入", "马上接入"):
        assert bad not in line, f"固定话术里出现了时效承诺: {bad}"


def test_prompt_forbids_time_and_money_promises():
    """prompt 侧硬规则(第二层)。长句仍会落到模型生成,所以这一层不能省。"""
    from app.prompts.agents import SAFETY_RULES
    assert "不许承诺时效与费用" in SAFETY_RULES
    assert "30秒内接入" in SAFETY_RULES
    assert "平台承担换货运费" in SAFETY_RULES
    assert "含糊但真实，好过具体但编造" in SAFETY_RULES or \
           "含糊但真实,好过具体但编造" in SAFETY_RULES
