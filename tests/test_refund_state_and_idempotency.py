"""退款:未支付的单不能退,并发申请只能成一次。

**实测缺陷**(走查并发与幂等时抓到,两条):

### ① `unpaid` 订单可以申请退款

    apply_refund(unpaid 订单) → 放行,状态改成 refund_processing
    对买家说:"退款申请已提交…预计 1-3 个工作日内审核完成，届时会通知您退货地址"

买家**一分钱都没付**,系统给他开了退款流程。更糟的是第二重后果:状态离开 `unpaid`
之后 `pay_order`(条件是 `status='unpaid'`)永远不再生效——**这笔单从此付不了款**,
而买家本来只是"不想买了"。而 `unpaid_flow_enabled=True` 时本地订单的**起始状态就是
unpaid**,这是默认路径,不是边角。

正确动作是**取消**,不是退款。但这里只拒绝并指路,不替买家改成取消:取消是另一个
动作、有自己的确认门,拿"退款"的授权去做"取消"等于绕过那道门。

### ② 先读后写:并发申请 16 次,12 次都报成功

`apply_refund` 读 `order["status"]` 判断,再调 `db.set_refund` 写——而改造前
`set_refund` 的 UPDATE 只有 `WHERE order_id = ?`,没有状态条件。于是状态机的守卫
全在那段"先读后写"里。实测 16 个并发,**12 个各自对买家说了一句"退款申请已提交"**。

修法:把状态条件下沉到 UPDATE 上(与 `pay_order` / `review_outreach_draft` /
`mark_outreach_sent` 同一套幂等纪律),并让 `apply_refund` **看 `set_refund` 的返回值**
——输掉竞态的那些如实转成"已有申请在处理中",不再继续往下走那句报喜的话。

顺带确认(同一轮实测,没有缺陷):
- `pay_order` 24 个并发 → 1 成功 / 23 拒绝 / 0 异常,条件更新在 SQLite 下确实只成一次。
"""

import os
import tempfile
import threading
from collections import Counter

import pytest

from app.agent.consent import consent_scope


@pytest.fixture()
def db(monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "auth_enabled", False)   # 归属校验是另一条测试的事
    import app.db as db_mod
    from app.db import Database

    d = Database(db_path=os.path.join(tempfile.mkdtemp(), "r.db"))
    d.init_schema()
    monkeypatch.setattr(db_mod, "_DB", d)
    return d


def _order(db, status, oid="O1", user="u1"):
    conn = db.connect()
    conn.execute("INSERT INTO orders (order_id,user,status,total) VALUES (?,?,?,?)",
                 (oid, user, status, 100.0))
    conn.commit()
    conn.close()
    return oid


# --------------------------------------------------------------------------
# ① 未支付不能退
# --------------------------------------------------------------------------

def test_unpaid_order_cannot_be_refunded(db):
    """**核心断言。** 修复前:放行 + 状态改成 refund_processing + 承诺退款。"""
    from app.agent.tools.refund import apply_refund

    _order(db, "unpaid")
    with consent_scope(["refund"]):
        r = apply_refund("O1", "不想要了")

    assert r["success"] is False, f"未付款却给退款: {r}"
    assert "尚未支付" in r["error"]
    assert db.get_order("O1")["status"] == "unpaid", "状态被改动了"


def test_unpaid_order_is_still_payable_after_refund_attempt(db):
    """**第二重后果**:退款一旦把状态改走,这笔单就永远付不了款了。

    这条比"承诺了不该有的退款"更难发现——买家点了退款、被告知在处理,然后发现
    自己既退不到钱(本来就没付)也付不了款,订单彻底卡死。
    """
    from app.agent.tools.refund import apply_refund

    _order(db, "unpaid")
    with consent_scope(["refund"]):
        apply_refund("O1", "不想要了")

    assert db.pay_order("O1", "u1") is True, "订单被退款流程卡死,付不了款"


def test_unpaid_refusal_points_to_cancel(db):
    """拒绝要给出路:告诉买家正确的动作是取消,而不是只说一句"不行"。

    但**不替他执行**——取消有自己的确认门,拿退款的授权去做取消等于绕过它。
    """
    from app.agent.tools.refund import apply_refund

    _order(db, "unpaid")
    with consent_scope(["refund"]):
        r = apply_refund("O1", "不想要了")
    assert "取消" in r["error"]
    assert db.get_order("O1")["status"] == "unpaid", "顺手把订单取消了——越权执行了另一个动作"


@pytest.mark.parametrize("status", ["pending", "shipped", "delivered"])
def test_refundable_statuses_still_pass(db, status):
    """**反向断言**:真正可退的状态必须照常放行,修复不能把退款功能掐掉。"""
    from app.agent.tools.refund import apply_refund

    _order(db, status)
    with consent_scope(["refund"]):
        r = apply_refund("O1", "不想要了")
    assert r["success"] is True, f"{status} 被误挡: {r}"
    assert db.get_order("O1")["status"] == "refund_processing"


@pytest.mark.parametrize("status,keyword", [
    ("refund_processing", "正在处理中"),
    ("cancelled", "已取消"),
])
def test_existing_guards_unchanged(db, status, keyword):
    from app.agent.tools.refund import apply_refund

    _order(db, status)
    with consent_scope(["refund"]):
        r = apply_refund("O1", "不想要了")
    assert r["success"] is False and keyword in r["error"]


# --------------------------------------------------------------------------
# ② 条件更新 + 并发
# --------------------------------------------------------------------------

def test_set_refund_is_conditional(db):
    """状态机守卫必须在 UPDATE 上,不能只在工具层的"先读后写"里。"""
    _order(db, "unpaid", oid="U1")
    _order(db, "shipped", oid="S1")

    assert db.set_refund("U1", "x") is False, "未支付的行被 set_refund 直接改了"
    assert db.get_order("U1")["status"] == "unpaid"
    assert db.set_refund("S1", "x") is True
    assert db.set_refund("S1", "x") is False, "第二次仍然生效 = 不幂等"


def test_unpaid_not_in_refundable_statuses(db):
    """把可退状态写成常量,方便别处引用而不是各自再抄一份判断。"""
    assert "unpaid" not in db.REFUNDABLE_STATUSES
    assert set(db.REFUNDABLE_STATUSES) == {"pending", "shipped", "delivered"}


def test_concurrent_refund_requests_succeed_once(db):
    """**修复前 16 个并发有 12 个报成功**,各自对买家说了一句"退款申请已提交"。"""
    from app.agent.tools.refund import apply_refund

    _order(db, "shipped")
    n = 16
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker():
        with consent_scope(["refund"]):
            barrier.wait()                      # 尽量让 n 个线程同一刻发起
            r = apply_refund("O1", "不想要了")
            with lock:
                results.append("ok" if r.get("success") else "blocked")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counts = Counter(results)
    assert counts["ok"] == 1, f"并发申请成功了 {counts['ok']} 次: {dict(counts)}"
    assert counts["blocked"] == n - 1


def test_loser_of_the_race_gets_honest_message(db, monkeypatch):
    """输掉竞态时说的必须是"已有申请在处理中",不能是那句报喜的"已提交"。"""
    from app.agent.tools.refund import apply_refund

    _order(db, "shipped")
    # 模拟"读到 shipped、写的时候已经被别人抢先":让 set_refund 返回 False
    monkeypatch.setattr(type(db), "set_refund", lambda self, oid, reason: False)
    with consent_scope(["refund"]):
        r = apply_refund("O1", "不想要了")
    assert r["success"] is False
    assert "正在处理中" in r["error"]


# --------------------------------------------------------------------------
# 顺带把"支付确实幂等"钉住(实测无缺陷,但它是同一类风险里最贵的一条)
# --------------------------------------------------------------------------

def test_concurrent_payment_succeeds_once(db):
    """24 个并发支付 → 恰好 1 次成功、0 个异常。

    实测本来就是对的(`pay_order` 用条件更新),但"重复扣款"是这一类里代价最高的
    一种,值得有一条测试长期把它按住,而不是靠"当时试过一次没事"。
    """
    _order(db, "unpaid")
    n = 24
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker():
        barrier.wait()
        try:
            ok = db.pay_order("O1", "u1")
            with lock:
                results.append("ok" if ok else "rejected")
        except Exception as exc:                # noqa: BLE001
            with lock:
                results.append("exc:" + type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counts = Counter(results)
    assert counts["ok"] == 1, dict(counts)
    assert all(not k.startswith("exc") for k in counts), f"并发下抛异常了: {dict(counts)}"
    assert db.get_order("O1")["status"] == "pending"
