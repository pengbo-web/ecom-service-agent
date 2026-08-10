"""支付必须与下单/列表走同一条路由规则。

实跑走查撞到:买家刚下的单显示「待支付」,点「去支付」拿到的却是
「支付失败:订单不存在、不属于当前用户,或已完成支付」。

根因:`POST /api/order` 对 demo 用户把订单建到 **hmdp**,`GET /api/orders` 也从
hmdp 读,而 `POST /api/order/{id}/pay` **只改本地库**——订单在 hmdp 里,本地库
根本没有这一行,于是支付永远失败。写入走一条路径、状态变更走另一条,与 MCP
那条(AI 读本地库、页面读 hmdp)是完全相同的形态。
"""

from __future__ import annotations

import inspect

from app.api import app as app_module


def _pay_source() -> str:
    src = inspect.getsource(app_module)
    start = src.index("def pay_order_endpoint")
    end = src.index('@app.post("/api/cart")', start)
    return src[start:end]


def test_pay_routes_through_hmdp_when_the_user_maps_there():
    """路由判据必须与下单/列表同一个:`_hmdp_token_for_user`。"""
    body = _pay_source()
    assert "_hmdp_token_for_user" in body, "支付要按与下单/列表相同的规则选后端"
    assert "/pay" in body, "应调用 hmdp 的支付接口"


def test_local_db_path_is_kept_for_non_demo_users():
    """非 demo 用户仍走本地库——修的是"漏了一条路径",不是"换掉原来那条"。"""
    body = _pay_source()
    assert "get_db().pay_order" in body


def test_status_is_read_back_from_upstream_not_guessed():
    """支付后回读真实状态,不在代码里假定"付完就是待发货"。

    状态归属上游,自己猜出来的值迟早和真相分叉。
    """
    body = _pay_source()
    assert "_fetch_hmdp_order" in body


def test_upstream_rejection_reason_is_surfaced():
    """上游的拒绝原因原样透出,不改写成一句含糊兜底。

    买家需要知道到底是"已支付"还是"不属于你"还是"不存在"。
    """
    body = _pay_source()
    assert 'd.get("errorMsg")' in body


def test_pay_uses_internal_client():
    """内网调用绕过系统代理,与其余 hmdp 调用同一套判定。"""
    body = _pay_source()
    assert "internal_client" in body


# ---------- 可评价列表:同一条"读一处、判另一处"的形态 ----------

def _reviewable_source() -> str:
    src = inspect.getsource(app_module)
    start = src.index("def reviewable(")
    end = src.index("@app.post(\"/api/review\")", start)
    return src[start:end]


def test_reviewable_is_derived_from_the_orders_the_buyer_actually_sees():
    """可评价列表必须与订单列表同源。

    改造前只查本地订单表,而 demo 用户的订单全在 hmdp——`reviewable_items`
    永远返回空,**整个评价功能对默认模式的买家不可达**。
    """
    body = _reviewable_source()
    assert "_hmdp_token_for_user" in body
    assert "_hmdp_my_orders" in body


def test_reviews_stay_on_the_agent_side():
    """评价本身仍存 agent 侧——hmdp 没有评价这个概念(只有 blog 评论)。

    所以正确的组合是"订单从买家实际看到的那份取,已评记录从本地取",
    而不是把评价也搬走。
    """
    body = _reviewable_source()
    assert "reviewed_pairs" in body


def test_reviewable_degrades_instead_of_500():
    """订单服务读不到时回落本地并标注,而不是整页报错。"""
    body = _reviewable_source()
    assert "degraded" in body
    assert "reviewable_items" in body, "回落路径要保留"
