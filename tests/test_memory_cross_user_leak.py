"""长期记忆不能把**别人的订单**念给这个买家听。

**实测泄漏**(走查安全时抓到,真实数据、真实回复,不是构造的假设):

`app/sessions/memory/1.json`(用户 `1` 的画像)里有一条 fact:

    订单ORD-20240115-001（物流单号SF1234567890）已发货，当前正在派送中，已抵达上海浦东区

而库里 `ORD-20240115-001` 属于用户 **小明**,`tracking_number` 就是 **SF1234567890**
——**是真数据**。以用户 `1` 的身份在界面上问这个单号,客服回:

    我注意到您名下有一笔已发货的Nike鞋订单:ORD-20240115-001 在历史记忆中曾被记录为
    "已发货,物流单号SF1234567890,正在派送中,已抵达上海浦东区"

**归属校验(`app/agent/tools/ownership.py`)挡住了工具调用,长期记忆绕过它,把另一个
买家的运单号和派送进度讲了出去,还说成"您名下"。**

全量审计 66 份画像:**49 份受影响,越权 fact 1 条,越权摘要 55 条。**摘要是大头,
多数写的是"系统未找到该订单"(没泄露什么),但也有 `default.json` 里
"客服确认该订单存在且已发货"这种——确认了别人订单的存在与状态。

污染是**一次性且永久**的:`owned_order` 的门控是"auth_enabled=False 放行",开发期
关掉鉴权跑过的对话会把当时看到的订单写进画像,之后把鉴权打开**不会**清掉已写入的东西。
所以修在**读取侧**:写入侧只能防新的,读取侧连已污染的画像一起兜住。
"""

import json

import pytest

from app.agent.memory.ownership_filter import (fact_belongs_to, filter_owned,
                                               referenced_order_ids)


# 现场原文,一个字都没改。
REAL_LEAK = "订单ORD-20240115-001（物流单号SF1234567890）已发货，当前正在派送中，已抵达上海浦东区"


# --------------------------------------------------------------------------
# 订单号识别:紧贴中文时也要认出来
# --------------------------------------------------------------------------

def test_order_id_glued_to_chinese_is_found():
    """**这条锁住我自己写错的第一版。**

    第一版用 `\\b[A-Z]...\\b` 做边界。而实际文本是「订单ORD-20240115-001（物流单号…」
    ——中文字符在 Python 的 Unicode 正则里**也算 word 字符**,`单` 与 `O` 之间根本没有
    `\\b`,于是整条 fact 被判成"没提到任何订单",**过滤器对真实泄漏完全无效**。
    审计脚本当时还因此报了一句"1.json 干净"。
    """
    assert referenced_order_ids(REAL_LEAK) == ["ORD-20240115-001"]


@pytest.mark.parametrize("text,expected", [
    ("订单ORD-20240115-001已发货", ["ORD-20240115-001"]),
    ("（ORD-20260802-66F8）", ["ORD-20260802-66F8"]),
    ("两笔:ORD-20260802-66F8和ORD-20260802-8A75", ["ORD-20260802-66F8", "ORD-20260802-8A75"]),
    ("ACC-SHIP-FRESH 已签收", ["ACC-SHIP-FRESH"]),
    ("偏好红褐色系服饰", []),
    ("", []),
])
def test_id_extraction_shapes(text, expected):
    assert referenced_order_ids(text) == expected


# --------------------------------------------------------------------------
# 归属判定
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "auth_enabled", True)
    from app.db import Database, set_db
    import app.db as db_mod
    d = Database(db_path=str(tmp_path / "ecom.db"))
    d.init_schema()
    # 直接写库:`create_order` 自己生成 order_id,而这里必须用现场那两个真实单号。
    conn = d.connect()
    conn.execute("INSERT INTO orders (order_id, user, status, total, tracking_number) "
                 "VALUES (?,?,?,?,?)",
                 ("ORD-20240115-001", "小明", "shipped", 899.0, "SF1234567890"))
    conn.execute("INSERT INTO orders (order_id, user, status, total) VALUES (?,?,?,?)",
                 ("ORD-20260802-66F8", "1", "pending", 5999.0))
    conn.commit()
    conn.close()
    monkeypatch.setattr(db_mod, "_DB", d)
    yield d
    set_db(None)


def test_real_leak_is_blocked_for_wrong_user(db):
    """**核心断言。** 用户 1 不该听到小明那笔单的任何内容。修复前这条会被注入。"""
    assert fact_belongs_to(REAL_LEAK, "1") is False


def test_owner_still_sees_own_order(db):
    """**反向断言**:小明自己问,这条必须留着。

    没有这条,修复就退化成"凡是提到订单的记忆一律不念",等于把长期记忆废掉。
    """
    assert fact_belongs_to(REAL_LEAK, "小明") is True


def test_own_order_passes(db):
    assert fact_belongs_to("订单ORD-20260802-66F8待发货", "1") is True


def test_unknown_order_id_is_kept(db):
    """库里查不到的单号照常保留——查不到就没有谁的隐私可泄,丢掉只是白损失记忆。"""
    assert fact_belongs_to("订单ORD-19990101-999 已取消", "1") is True


def test_fact_without_order_id_is_kept(db):
    assert fact_belongs_to("偏好红褐色系服饰", "1") is True


def test_mixed_fact_is_dropped(db):
    """一条 fact 同时提到自己的和别人的订单 → 丢。

    自由文本没法可靠地只抹掉其中一半:把单号删了,"已抵达上海浦东区"这种同样属于
    别人的信息还留在句子里。
    """
    assert fact_belongs_to("我的ORD-20260802-66F8和ORD-20240115-001都要查", "1") is False


def test_db_failure_is_fail_closed(db, monkeypatch):
    """查询本身抛错 → 不念(与 `owned_order` 拿不到身份即拒同理)。

    记忆缺一段只是这一轮答得笼统,念错人的订单是数据泄漏,两者不对等。
    """
    import app.agent.memory.ownership_filter as of
    monkeypatch.setattr(of, "_owner_of", lambda oid: (_ for _ in ()).throw(RuntimeError("db down")))
    assert fact_belongs_to(REAL_LEAK, "1") is False


# --------------------------------------------------------------------------
# 门控:与 owned_order 同一套
# --------------------------------------------------------------------------

def test_auth_disabled_does_not_filter(db, monkeypatch):
    """`auth_enabled=False` 是单机教学模式,没有"归属"这回事,保持既有行为。

    与 `app/agent/tools/ownership.py` 的门控严格一致——两处判据分叉会让"到底拦不拦"
    变成一个要读两个模块才能回答的问题。
    """
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    kept, dropped = filter_owned([REAL_LEAK], "1")
    assert kept == [REAL_LEAK] and dropped == []


def test_filter_reports_what_it_dropped(db):
    """拦掉多少要能被观测到:静默过滤会让"记忆里怎么少了一条"变成查不动的问题。"""
    kept, dropped = filter_owned([REAL_LEAK, "偏好红褐色系服饰"], "1")
    assert kept == ["偏好红褐色系服饰"]
    assert dropped == [REAL_LEAK]


# --------------------------------------------------------------------------
# 两条出口都要堵:自动注入 + 模型主动调的 recall_user_memory
# --------------------------------------------------------------------------

def _ltm(tmp_path, user_id, facts, summaries=()):
    from app.agent.memory.long_term import LongTermMemory, MemoryFact
    m = LongTermMemory(memory_dir=str(tmp_path / "mem"), user_id=user_id)
    m.facts = [MemoryFact(content=c, category="behavior", created_at="2026-08-12T00:00:00") for c in facts]
    m.interaction_summaries = [{"summary": s, "at": "2026-08-12"} for s in summaries]
    return m


def test_prompt_section_excludes_other_users_facts(db, tmp_path):
    """自动注入这条出口:拼进 system prompt 之前必须已经筛过。"""
    ltm = _ltm(tmp_path, "1", [REAL_LEAK, "偏好红褐色系服饰"])
    text = ltm.build_prompt_section() or ""
    assert "SF1234567890" not in text, "运单号进了提示词"
    assert "ORD-20240115-001" not in text
    assert "偏好红褐色系服饰" in text, "把不相干的记忆也筛掉了"


def test_prompt_section_excludes_other_users_summaries(db, tmp_path):
    """摘要是大头(实测 55 条 vs 1 条),不能只筛 facts。"""
    ltm = _ltm(tmp_path, "1", [],
               ["用户查询订单 ORD-20240115-001 状态，客服确认该订单存在且已发货",
                "用户咨询了退货运费规则"])
    text = ltm.build_prompt_section() or ""
    assert "ORD-20240115-001" not in text
    assert "退货运费" in text


def test_owner_prompt_section_keeps_everything(db, tmp_path):
    ltm = _ltm(tmp_path, "小明", [REAL_LEAK])
    assert "SF1234567890" in (ltm.build_prompt_section() or "")


def test_recall_tool_excludes_other_users_facts(db, tmp_path, monkeypatch):
    """模型可以直接调 `recall_user_memory` —— 漏在这里等于前一道白做。"""
    from types import SimpleNamespace

    import app.agent.tools.memory_tool as mt

    ltm = _ltm(tmp_path, "1", [REAL_LEAK, "偏好红褐色系服饰"],
               ["用户查询订单 ORD-20240115-001 状态，客服确认该订单存在且已发货"])
    manager = SimpleNamespace(memory_enabled=True, ltm=ltm,
                              stm=SimpleNamespace(facts=[]))
    mt.set_memory_manager(manager)
    try:
        out = mt.recall_user_memory("订单")
    finally:
        mt.set_memory_manager(None)

    blob = json.dumps(out, ensure_ascii=False)
    assert "SF1234567890" not in blob
    assert "ORD-20240115-001" not in blob
    assert "偏好红褐色系服饰" in blob


def test_filter_failure_does_not_crash_prompt_build(db, tmp_path, monkeypatch):
    """过滤器自己炸了不能让整轮对话挂掉——但也不能因此把记忆放行。"""
    import app.agent.memory.ownership_filter as of
    monkeypatch.setattr(of, "filter_owned",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ltm = _ltm(tmp_path, "1", [REAL_LEAK])
    assert ltm.build_prompt_section() in (None, "")
