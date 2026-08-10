"""订单清单工具的两条诚实性约束。

两条都是实跑走查抓到的,且都属于同一类:**用同一种表示承载两种不同的事实**。

① 上游失败时返回 `success:True + 空列表` —— Agent 被告知"这位买家一笔订单都
   没有",于是自信地对买家说"您名下没有订单",而真相是订单服务没连上。
② 概要清单静默丢掉物流字段 —— Agent 分不清"这单没有物流单号"和"这份清单不带
   物流单号",实测它对买家说出「系统在**多次查询**中均未匹配到对应物流单号」,
   而它一次物流都没查过(调用链里只有一次 list_user_orders),该单在 hmdp 里也
   确实有单号 SF1234567890/顺丰。
"""

from __future__ import annotations

import json

import pytest


# ---------- MCP 版 ----------

class _Client:
    def __init__(self, res):
        self._res = res

    def get_json(self, path, params=None, token=None):
        return self._res


@pytest.fixture()
def mcp_mod(monkeypatch):
    from mcp_server import hmdp_server

    return hmdp_server


def test_upstream_failure_is_not_reported_as_zero_orders(mcp_mod, monkeypatch):
    """上游失败必须报错,不能伪装成"这位买家没有订单"。

    这是本次走查里最危险的一条:它不会让任何东西崩,只会让客服对着买家说一句
    确凿的假话。旁边的 `_query_order_impl` / `_list_products_impl` 都是先判
    success 再走,只有这一个漏了。
    """
    monkeypatch.setattr(mcp_mod, "_client",
                        _Client({"success": False, "errorMsg": "无法连接 hmdp:ConnectError"}))
    out = json.loads(mcp_mod._list_user_orders_impl(ctx_user_id="1", ctx_token="t"))
    assert out["success"] is False
    assert "orders" not in out or not out["orders"]
    assert out["error"], "必须给出原因,而不是一个空列表"


def test_empty_list_from_healthy_upstream_is_still_success(mcp_mod, monkeypatch):
    """真的没有订单时仍是成功——不能矫枉过正把空当故障。"""
    monkeypatch.setattr(mcp_mod, "_client", _Client({"success": True, "data": []}))
    out = json.loads(mcp_mod._list_user_orders_impl(ctx_user_id="1", ctx_token="t"))
    assert out["success"] is True
    assert out["count"] == 0


def test_brief_marks_whether_tracking_exists(mcp_mod, monkeypatch):
    """概要要能区分"这单没单号"与"清单不带单号"。"""
    monkeypatch.setattr(mcp_mod, "_client", _Client({"success": True, "data": [
        {"order_no": "A1", "user_id": 1, "status": "shipped", "total": 89900,
         "tracking_number": "SF1234567890", "items": []},
        {"order_no": "A2", "user_id": 1, "status": "pending", "total": 100, "items": []},
    ]}))
    out = json.loads(mcp_mod._list_user_orders_impl(ctx_user_id="1", ctx_token="t"))
    by_id = {o["order_id"]: o for o in out["orders"]}
    assert by_id["A1"]["has_tracking"] is True
    assert by_id["A2"]["has_tracking"] is False
    # 并且明说这是概要,物流要单独查
    assert "query_logistics" in out["note"]


def test_no_user_still_rejected(mcp_mod):
    out = json.loads(mcp_mod._list_user_orders_impl(ctx_user_id="", ctx_token="t"))
    assert out["success"] is False


# ---------- 本地工具版(MCP 不可用时的降级路径) ----------

def test_local_tool_has_the_same_contract(monkeypatch, tmp_path):
    """两条路径的返回语义必须一致。

    MCP 连不上时 Agent 会静默切到本地工具(见 app/mcp_client/client.py 的注释)。
    如果两边契约不同,同一个买家问同一句话,得到的答案形状会随一次网络抖动而变。
    """
    from app.agent.tools import user_orders as mod
    from app.config.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", False)

    class _DB:
        def list_orders(self):
            return [
                {"order_id": "A1", "status": "shipped", "total": 899.0,
                 "created_at": "2026-08-01", "items": [{"name": "鞋"}],
                 "tracking_number": "SF1"},
                {"order_id": "A2", "status": "pending", "total": 10.0,
                 "created_at": "2026-08-02", "items": [{"name": "袜"}]},
            ]

    monkeypatch.setattr(mod, "get_db", lambda: _DB())
    out = mod.list_user_orders()
    by_id = {o["order_id"]: o for o in out["orders"]}
    assert by_id["A1"]["has_tracking"] is True
    assert by_id["A2"]["has_tracking"] is False
    assert "query_logistics" in out["note"]
