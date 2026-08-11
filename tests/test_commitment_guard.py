"""买家回复里许了钱、而这一轮没有政策依据——必须被看见。

**实测缺陷。** 买家问的是尺码,客服顺口答:

    「…如不合适可免费换货（支持7天无理由，平台承担换货运费）」

平台真实政策是"若已签收且属七天无理由退货,**运费由您承担**(约12元起)"——方向
相反的对客金钱承诺。

三道既有防线为什么都没挡住:

1. prompt 里"涉及费用必须先检索知识库"只在模型**自认为在讲政策**时才起作用,
   而这句是顺口带出来的;
2. `COMMITMENT_KEYWORDS` 这份词表只用在 skill 风险分级 / 触达草稿 / 评价关键词
   三处——**买家回复这条路上一个金钱承诺检查都没有**;
3. 即便用上,朴素子串匹配也漏:词表里是 `承担运费`,回复写的是"承担**换货**运费"。

第 3 点最值得记。`growth.py` 里那段注释解释过为什么当时能接受朴素匹配——
"这个残余漏洞是可接受的,因为每条草稿都必须经人工审批才会发出"。**这个理由在买家
回复上不成立**:那条路没有人工闸。把一个缓解措施连同它的前提一起搬到别处,是这类
问题的常见成因。
"""

import pytest

from app.agent.commitment_guard import detect_unbacked_commitment, find_commitments


# --------------------------------------------------------------------------
# 匹配:必须容忍中间插词(这正是实测漏掉的那一种)
# --------------------------------------------------------------------------

def test_catches_the_real_world_miss():
    """实测那句。朴素子串表里的 `承担运费` 匹配不上"承担换货运费"。"""
    hits = find_commitments("如不合适可免费换货（支持7天无理由，平台承担换货运费）")
    assert "承担运费" in hits


@pytest.mark.parametrize("text", [
    # 动作在前
    "平台承担运费",
    "我们承担退货运费",
    "商家承付快递费",
    "店铺负担邮费",
    # 费用在前——这个语序是被本文件的测试抓出来的,第一版正则只覆盖了上面那种
    "运费由平台承担",
    "换货邮费由我们承担",
    "快递费由本店负担",
])
def test_shipping_fee_variants(text):
    assert "承担运费" in find_commitments(text)


@pytest.mark.parametrize("text,expected", [
    ("这单包邮的", "免运费"),
    ("运费全免", "免运费"),
    ("可以免费退货", "免费换退"),
    ("我们先行赔付", "赔付补偿"),
    ("给您全额退款", "全额退"),
    ("再补发一张优惠券", "补券"),
])
def test_other_categories(text, expected):
    assert expected in find_commitments(text)


def test_只有店铺侧承担才算承诺():
    """**主语决定性质。** "运费由您承担"是如实转述平台政策(正确行为),
    "运费由平台承担"是对客金钱承诺——两句结构几乎一样,差别只在主语。不区分就会把
    正确行为也标出来,而一道总在响的检测等于没有检测。"""
    # 买家承担 = 政策转述,不报
    assert find_commitments("七天无理由退货的运费由您承担，约12元起") == []
    assert find_commitments("这种情况运费需要买家自行承担") == []
    # 店铺承担 = 承诺,要报
    assert find_commitments("如因质量问题退货，运费由平台承担") != []
    assert find_commitments("平台承担换货运费") != []


def test_没有主语时不猜():
    """裸"承担运费"没有主语,判不出是谁承担——不报。

    宁可漏一条也不误报:这道检测的价值全在"响的时候值得看",而误报会让它很快被
    当成噪音忽略掉(与 anomaly 那次"已修好的工具连续报警 7 天"同一个教训)。
    真实场景里客服说这句话时几乎总会带上主语。
    """
    assert find_commitments("承担运费") == []


def test_no_false_positive_on_plain_reply():
    assert find_commitments("您的订单已发货，物流单号 SF1234567890") == []
    assert find_commitments("") == []
    assert find_commitments(None) == []


# --------------------------------------------------------------------------
# 合取判定:说了钱 且 没有依据
# --------------------------------------------------------------------------

REPLY = "如不合适可免费换货（支持7天无理由，平台承担换货运费）"


def test_flags_when_not_grounded():
    assert detect_unbacked_commitment(REPLY, kb_grounded=False)


def test_silent_when_grounded():
    """检索到政策后如实转述运费规则是**正确行为**,不该被标出来。

    只看"说了钱"这一个信号会把正确行为也报出来——那是噪音,而一道总在响的检测
    等于没有检测(与 anomaly 那次"已修好的工具连续报警 7 天"同一个教训)。
    """
    assert detect_unbacked_commitment(REPLY, kb_grounded=True) == []


def test_silent_when_no_commitment():
    assert detect_unbacked_commitment("您的订单已发货", kb_grounded=False) == []


# --------------------------------------------------------------------------
# 接线:必须真的挂在出话之后
# --------------------------------------------------------------------------

def test_wired_into_chat_after_reply():
    """钉住接线,否则上面全绿也没用。"""
    import ast
    import inspect
    import app.agent.chat as chat_mod

    tree = ast.parse(inspect.getsource(chat_mod))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "chat"), None)
    assert fn is not None
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_check_unbacked_commitment" in called, (
        "检测器没挂在 chat() 的出话路径上——回复里的金钱承诺仍然没人看")


def test_event_shape_matches_the_guard_channel():
    """发的事件形状必须与 tracer 的 guard 分支对得上(kind + hits)。

    今天早些时候踩过一次:检测器命中了,但 tracer 只读 `guard` 键,于是 hits 整个
    被丢掉、落成一条 `guard:None` 的空 span,排查时看起来像"检测器没工作"。
    """
    from app.observability.store import TraceStore
    from app.observability.tracer import Tracer

    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        store = TraceStore(os.path.join(td, "t.db"))
        store.init_schema()
        tr = Tracer(store)
        with tr.start_trace("s-1", "尺码准吗") as t:
            tr.on_event({"type": "guard", "kind": "unbacked_commitment",
                         "hits": ["承担运费"]})
        sp = [s for s in store.get_trace(t.trace_id)["spans"]
              if s["kind"] == "guard"][0]
        assert sp["name"] == "guard:unbacked_commitment"
        assert (sp["meta"] or {}).get("hits") == ["承担运费"]
