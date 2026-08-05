"""评测闭环:人工回复→评测期望;回流用例→回归集(不覆盖人工维护的用例)。"""

import json

from app.evaluation.trace_to_case import (
    extract_human_reply, keywords_from_reply, trace_to_case,
)
from app.evaluation.case_merge import merge_cases
from app.agent.skills.golden_corpus import HUMAN_AGENT_INTENT


def _archived(*replies):
    msgs = []
    for intent, text in replies:
        msgs.append({"role": "user", "content": "问题"})
        msgs.append({"role": "assistant",
                     "content": json.dumps({"intent": intent, "reply": text},
                                           ensure_ascii=False)})
    return {"messages": msgs}


def test_extract_human_reply_picks_the_human_one():
    a = _archived(("product_consult", "AI 的回答"),
                  (HUMAN_AGENT_INTENT, "您的退款我已经手工加急,今天内到账"))
    assert "手工加急" in extract_human_reply(a)


def test_extract_human_reply_takes_the_last_when_several():
    a = _archived((HUMAN_AGENT_INTENT, "第一次人工回复"),
                  (HUMAN_AGENT_INTENT, "第二次人工回复才是最终答案"))
    assert "第二次" in extract_human_reply(a)


def test_extract_human_reply_empty_without_human():
    assert extract_human_reply(_archived(("product_consult", "只有 AI"))) == ""


def test_extract_tolerates_non_json_assistant():
    a = {"messages": [{"role": "assistant", "content": "纯文本旧格式"}]}
    assert extract_human_reply(a) == ""


# ---------- Finding 1:extract_human_reply 必须扛住 get_archived_session 的真实形状 ----------
# Database.get_archived_session 返回 dict(row),messages 是**未反序列化的 JSON 字符串**
# (它的兄弟方法 list_recent_archives 才会 json.loads)。上面几个测试手工构造的 archived
# 全是"messages 已经是 list"的形状,不会暴露这个问题——下面用真实的 archive_session +
# get_archived_session 往返,拿到生产环境真正会给到的形状。

def test_extract_human_reply_handles_raw_db_row(tmp_path):
    """真实 DB 往返:messages 是 JSON 字符串,不是已解析的 list。"""
    from app.db.database import Database

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    messages = [
        {"role": "user", "content": "退款怎么还没到"},
        {"role": "assistant", "content": json.dumps(
            {"intent": HUMAN_AGENT_INTENT, "reply": "您的退款我已经手工加急,今天内到账"},
            ensure_ascii=False)},
    ]
    db.archive_session("s1", "alice", messages, None)

    archived = db.get_archived_session("s1")
    assert isinstance(archived["messages"], str)  # 确认拿到的确实是 DB 的真实形状

    assert "手工加急" in extract_human_reply(archived)


def test_extract_human_reply_tolerates_unparseable_messages_string():
    """messages 字符串本身就不是合法 JSON(脏数据)时不炸,按空处理。"""
    assert extract_human_reply({"messages": "not json {{{"}) == ""


def test_extract_human_reply_tolerates_messages_of_wrong_type():
    """messages 既不是 str 也不是 list(比如意外传了个 dict/None)时同样不炸。"""
    assert extract_human_reply({"messages": {"unexpected": "shape"}}) == ""
    assert extract_human_reply({"messages": None}) == ""
    assert extract_human_reply({}) == ""


# ---------- Finding 2:关键词只认系统已知词表,不再对回复任意分词 ----------

def test_keywords_from_reply_returns_vocabulary_terms_that_actually_recur():
    """回复里出现的状态标签/承诺类术语被抽出来;它们短、有实义、能在改写后的
    表述里重新出现,不像旧版切出来的整句长子句只能靠逐字复现才算命中。"""
    reply = "您的订单已经从待发货变为已发货,退款我们决定全额退,并补偿一张代金券,请注意查收"
    kws = keywords_from_reply(reply)
    assert kws
    assert all(2 <= len(k) <= 10 for k in kws)
    assert set(kws) <= {"已发货", "待发货", "全额退", "代金券", "补偿"}
    assert "已发货" in kws  # 至少命中一个真实存在的状态标签


def test_keywords_from_reply_rejects_digit_like_vocabulary(monkeypatch):
    """词表里若混入带数字的词(比如某商品名带具体型号),必须被剔除——
    订单号/日期/型号这类东西不可能在下一次人工回复里原样复现。"""
    class _FakeDb:
        def all_products(self):
            return [{"name": "小米14手机"}, {"name": "蓝牙耳机"}]

    monkeypatch.setattr("app.db.get_db", lambda: _FakeDb())
    reply = "已经为您安排发货,小米14手机和蓝牙耳机都会一起寄出"
    kws = keywords_from_reply(reply)
    assert "小米14手机" not in kws       # 含数字,剔除
    assert "蓝牙耳机" in kws            # 不含数字,允许命中


def test_keywords_from_empty_reply_is_empty():
    """抽不出就留空——空期望在评分时被跳过,编造才危险。"""
    assert keywords_from_reply("") == []
    assert keywords_from_reply("好的~") == []


def test_last_human_reply_being_a_courtesy_line_yields_no_fake_keywords():
    """"最后一条人工回复"有时是客套收尾而非最具体那条回答;新版关键词抽取
    对此天然免疫——客套话匹配不到任何词表词,返回 []而不是编一个假期望。"""
    a = _archived((HUMAN_AGENT_INTENT, "您的订单已经从待发货变为已发货,请查收"),
                  (HUMAN_AGENT_INTENT, "不客气,还有其他问题欢迎随时联系"))
    reply = extract_human_reply(a)
    assert "不客气" in reply           # 确实取到的是最后一条(客套结束语)
    assert keywords_from_reply(reply) == []  # 但没有产生一个只能靠运气命中的假期望


def test_trace_to_case_carries_human_keywords():
    trace = {"trace_id": "abcdef123", "user_input": "退款怎么还没到",
             "intent": "refund", "spans": [{"kind": "hitl"}]}
    case = trace_to_case(
        trace, human_reply="您的订单已经从待发货变为已发货,我们决定全额退")
    assert case["expected_requires_human"] is True
    assert case["expected_keywords"]


def test_trace_to_case_without_human_reply_has_no_keywords():
    """回归保护:不传人工回复时行为与改造前一致。"""
    trace = {"trace_id": "abc", "user_input": "问题", "intent": "refund", "spans": []}
    case = trace_to_case(trace)
    assert "expected_keywords" not in case or case["expected_keywords"] == []


def test_merge_adds_new_cases():
    existing = [{"id": "hand-1", "description": "人工维护"}]
    incoming = [{"id": "reflow-a", "description": "回流"}]
    merged, added = merge_cases(existing, incoming)
    assert added == 1
    assert {c["id"] for c in merged} == {"hand-1", "reflow-a"}


def test_merge_never_overwrites_existing_id():
    """人工维护的用例是资产,回流不得覆盖它。"""
    existing = [{"id": "hand-1", "description": "人工维护的原始期望"}]
    incoming = [{"id": "hand-1", "description": "回流想覆盖"}]
    merged, added = merge_cases(existing, incoming)
    assert added == 0
    assert merged[0]["description"] == "人工维护的原始期望"


def test_merge_dedupes_within_incoming():
    merged, added = merge_cases([], [{"id": "x"}, {"id": "x"}])
    assert added == 1 and len(merged) == 1


def test_merge_skips_incoming_case_with_missing_or_empty_id():
    """incoming 里没有 id / id 为空串的用例被静默丢弃——这是显式行为,不是疏漏,
    必须有测试盯住它,否则一次重构就可能悄悄把它变成"抛异常"或"当空 id 收下"。"""
    existing = [{"id": "hand-1", "description": "人工维护"}]
    incoming = [{"description": "没有 id 字段"}, {"id": "", "description": "id 是空串"}]
    merged, added = merge_cases(existing, incoming)
    assert added == 0
    assert merged == existing
