"""协作起草并行(L2)+ 草稿去重下沉数据层(P2)。

两件事必须一起测:并行是**目的**,数据层判重是**它的前提**——没有约束顶着,
并行会把"先查后插"这个在串行下看不出问题的写法直接变成重复草稿。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from unittest.mock import patch

import pytest

from app.config.settings import settings
from app.db.database import Database
from app.multi_agent import bus, collab, shared_context as sc


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "par.db"))
    d.init_schema()
    from app.db import set_db
    set_db(d)          # 换全局单例即可:各模块都在调用时才 get_db()
    yield d
    set_db(None)


def _unpaid(d, oid, user):
    conn = d.connect()
    conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                 "VALUES (?,?,'unpaid',199,datetime('now','-3 days'))", (oid, user))
    conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                 "VALUES (?, '跑鞋','P001',1,199)", (oid,))
    conn.commit()
    conn.close()


def _signal(d, corr="C1"):
    return d.publish_event(bus.EV_SIGNAL_ANOMALY, {
        "kind": "refund_rate_high", "subject": "P001", "subject_name": "跑鞋",
        "value": 0.3, "threshold": 0.15, "detail": {"orders": 20, "refunds": 6},
    }, bus.AGENT_SERVICE, bus.AGENT_ANALYST, corr)


# ---------- P2:唯一约束 ----------

def test_duplicate_draft_returns_none_not_raises(db):
    """撞唯一约束返回 None(不是错误,是去重生效)——与 start_followup 同一约定。"""
    first = db.create_outreach_draft("unpaid_order", "u1", "O1", "内容", {}, "", "C1", "growth")
    second = db.create_outreach_draft("unpaid_order", "u1", "O1", "换个说法", {}, "", "C2", "growth")
    assert isinstance(first, int)
    assert second is None
    assert len(db.list_outreach_drafts(status="draft", limit=10)) == 1


def test_constraint_only_covers_draft_status(db):
    """约束只管待审的那些:批准发出之后,同一订单可以再次被触达。

    approved/sent 是"已经处理过的历史",不该永久封杀;这与
    pending_outreach_targets 的既有口径一致,不新造第二套语义。
    """
    first = db.create_outreach_draft("unpaid_order", "u1", "O1", "第一次", {}, "", "C1", "growth")
    db.review_outreach_draft(first, "approved", reviewed_by="admin")
    db.mark_outreach_sent(first)
    again = db.create_outreach_draft("unpaid_order", "u1", "O1", "第二次", {}, "", "C2", "growth")
    assert isinstance(again, int)


def test_constraint_is_per_kind_and_per_order(db):
    """不同商机类型 / 不同订单是不同的事,不该互相封杀。"""
    assert db.create_outreach_draft("unpaid_order", "u1", "O1", "a", {}, "", "C1", "growth")
    assert db.create_outreach_draft("abandoned_cart", "u1", "O1", "b", {}, "", "C2", "growth")
    assert db.create_outreach_draft("unpaid_order", "u1", "O2", "c", {}, "", "C3", "growth")


def test_concurrent_create_only_one_wins(db):
    """并行起草的核心保证:两个线程同时给同一买家排同一件事,只落一条。

    这正是应用层 seen 集合挡不住的那一类——两个线程各自读到"当前没有",
    先查后插必然双双通过。
    """
    results: list = []
    lock = threading.Lock()

    def _create():
        r = db.create_outreach_draft("unpaid_order", "u9", "O9", "内容", {}, "", "C", "growth")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_create) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r is not None) == 1
    assert sum(1 for r in results if r is None) == 1


def test_existing_duplicates_are_collapsed_on_init_schema(tmp_path):
    """存量重复必须先收敛,否则建唯一索引会让**服务起不来**。

    收敛保留 id 最大的一条(最新内容最贴近当前情境),其余置 rejected 而不
    物理删除——审计要能看到"这条曾经存在过、因为什么被收掉"。
    """
    path = str(tmp_path / "legacy.db")
    d = Database(db_path=path)
    d.init_schema()
    # 绕过约束造存量重复:先删索引,插入,再重跑 init_schema 模拟升级
    conn = d.connect()
    conn.execute("DROP INDEX IF EXISTS idx_outreach_draft_unique")
    for txt in ("旧一", "旧二", "旧三"):
        conn.execute(
            "INSERT INTO outreach_drafts (opportunity_type,user_id,order_id,content,"
            "offer,reason,correlation_id,status,created_by,created_at) "
            "VALUES ('unpaid_order','u1','O1',?,'{}','','C','draft','growth',datetime('now'))",
            (txt,))
    conn.commit()
    conn.close()

    Database(db_path=path).init_schema()          # 升级:应收敛而不是崩

    d2 = Database(db_path=path)
    drafts = d2.list_outreach_drafts(status="draft", limit=10)
    assert len(drafts) == 1 and drafts[0]["content"] == "旧三"    # 留最新那条
    rejected = d2.list_outreach_drafts(status="rejected", limit=10)
    assert len(rejected) == 2                                     # 其余留痕,未删除
    assert all("历史重复草稿" in (r.get("needs_review_reason") or "") for r in rejected)


def test_draft_tool_tells_model_not_to_retry(db):
    """工具层返回的错误必须明说"重试也没用"——否则模型的第一反应是换个措辞再调。"""
    from app.agent.tools.growth import draft_outreach
    draft_outreach(user_id="u1", content="第一条话术内容", kind="unpaid_order", order_id="O1")
    again = draft_outreach(user_id="u1", content="换个说法的话术", kind="unpaid_order", order_id="O1")
    assert again["success"] is False
    assert "无需重复起草" in again["error"] and "重试" in again["error"]


# ---------- L2:并行 ----------

def test_parallel_and_serial_produce_the_same_drafts(db, monkeypatch):
    """关开关必须逐字节回退:并行只改"多快",不改"产出什么"。"""
    for i in range(4):
        _unpaid(db, f"O{i}", f"u{i}")
    _signal(db, corr="CP")

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="给您留意了一下"):
        monkeypatch.setattr(settings, "collab_parallel_enabled", False)
        collab.run_once()
    serial = {(d["user_id"], d["opportunity_type"]) for d in
              db.list_outreach_drafts(status="draft", limit=50)}
    assert len(serial) == 4


def _concurrency_probe():
    """返回 (打桩起草函数, 状态字典)。状态里记"当前在飞数"与历史峰值。"""
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def _tracked(_diag, _opp):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        try:
            time.sleep(0.05)      # 给其它线程机会真正重叠进来
            return "话术"
        finally:
            with lock:
                state["inflight"] -= 1

    return _tracked, state


def test_drafting_actually_runs_concurrently(db, monkeypatch):
    """起草确实并发:直接测**同时在飞的起草数**,不测挂钟时间。

    这条一开始写成了"8 条 ×0.15s 的挂钟应低于 0.9 秒",实测证明那个写法是错的:
    同一文件里先跑过并发写的用例之后,SQLite 写锁竞争让每条落库变慢,挂钟反而
    **超过**串行(实测 3.34s vs 串行理论 1.2s),于是这条测试既会因为机器慢而
    假红,也会因为并行度不足但机器快而假绿——**它测的是环境,不是被测行为**。

    改成数并发度:用一把锁维护"当前在飞数"与历史峰值,峰值 > 1 就证明确实并发,
    与机器速度、磁盘竞争完全无关。
    """
    for i in range(8):
        _unpaid(db, f"O{i}", f"u{i}")
    _signal(db, corr="CT")

    tracked, state = _concurrency_probe()
    monkeypatch.setattr(settings, "collab_max_parallel", 4)
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", side_effect=tracked):
        collab.run_once()

    assert len(db.list_outreach_drafts(status="draft", limit=50)) == 8
    assert state["peak"] > 1, "起草没有并发(峰值在飞数为 1)"
    assert state["peak"] <= 4, f"并发度超过了 collab_max_parallel(峰值 {state['peak']})"


def test_serial_mode_has_no_concurrency(db, monkeypatch):
    """关掉开关必须真的回到串行——峰值在飞数恒为 1。"""
    for i in range(4):
        _unpaid(db, f"O{i}", f"u{i}")
    _signal(db, corr="CS")

    tracked, state = _concurrency_probe()
    monkeypatch.setattr(settings, "collab_parallel_enabled", False)
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", side_effect=tracked):
        collab.run_once()

    assert state["peak"] == 1


def test_one_failing_draft_does_not_kill_the_batch(db, monkeypatch):
    """单条起草抛异常只跳过那一条——并行下这条语义必须与串行版一致。"""
    for i in range(4):
        _unpaid(db, f"O{i}", f"u{i}")
    _signal(db, corr="CF")

    calls = {"n": 0}
    lock = threading.Lock()

    def _flaky(_diag, _opp):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n == 2:
            raise RuntimeError("模型抖动")
        return "话术"

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", side_effect=_flaky):
        collab.run_once()

    assert len(db.list_outreach_drafts(status="draft", limit=50)) == 3


def test_parallel_path_still_dedupes(db, monkeypatch):
    """并行路径下去重仍然生效:已有待审草稿的买家不会被再排一条。"""
    _unpaid(db, "O1", "u1")
    db.create_outreach_draft("unpaid_order", "u1", "O1", "已经躺着一条", {}, "", "PRE", "growth")
    _signal(db, corr="CD")

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="新话术"):
        collab.run_once()

    drafts = db.list_outreach_drafts(status="draft", limit=50)
    assert len(drafts) == 1 and drafts[0]["content"] == "已经躺着一条"
