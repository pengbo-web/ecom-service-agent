"""hmdp-mcp 工具:mock 掉 HmdpClient,验证映射/归属/底价剔除,不触网。"""

import json
from unittest.mock import patch

import mcp_server.hmdp_server as srv


def test_query_product_hides_floor():
    fake = {"success": True, "data": [{"id": 2, "title": "小米14 Ultra 手机",
            "category": "手机", "price": 599900, "floorPrice": 560000, "stock": 42,
            "sku": "X", "description": "d", "specs": "{}"}]}
    with patch.object(srv._client, "get_json", return_value=fake):
        out = json.loads(srv._query_product_impl("手机", ctx_user_id="1"))
    assert out["success"] and out["products"][0]["price"] == 5999.0
    assert "floor_price" not in out["products"][0]


def test_query_order_ownership():
    order = {"success": True, "data": {"order_no": "ORD-1", "user_id": 1, "status": "shipped",
             "total": 89900, "items": []}}
    with patch.object(srv._client, "get_json", return_value=order):
        ok = json.loads(srv._query_order_impl("ORD-1", ctx_user_id="1"))
        bad = json.loads(srv._query_order_impl("ORD-1", ctx_user_id="999"))
    assert ok["success"] and ok["order"]["status"] == "shipped"
    assert bad["success"] is False        # 非本人 → 查无此单


def test_query_logistics_via_order():
    order = {"success": True, "data": {"order_no": "ORD-1", "user_id": 1, "status": "shipped",
             "total": 89900, "tracking_number": "SF1", "items": []}}
    lg = {"success": True, "data": {"tracking_number": "SF1", "carrier": "顺丰",
          "status": "in_transit", "events": [{"time": "t", "location": "l", "description": "d"}]}}

    def fake_get(path, params=None, token=None):
        return lg if path.startswith("/logistics/") else order

    with patch.object(srv._client, "get_json", side_effect=fake_get):
        out = json.loads(srv._query_logistics_impl("ORD-1", ctx_user_id="1"))
    assert out["success"] and out["logistics"]["events"][0]["description"] == "d"


def test_query_order_not_found():
    with patch.object(srv._client, "get_json", return_value={"success": False, "data": None}):
        out = json.loads(srv._query_order_impl("ORD-x", ctx_user_id="1"))
    assert out["success"] is False


# ---- Task 2.3: 写工具 + 议价 ----
def test_apply_refund_maps_hmdp_result():
    with patch.object(srv._client, "post_json",
                      return_value={"success": True, "data": "退款申请已提交，预计1-3个工作日审核"}):
        out = json.loads(srv._apply_refund_impl("ORD-1", "不想要了", ctx_user_id="1"))
    assert out["success"] is True and "退款" in out["message"]


def test_apply_refund_hmdp_reject():
    with patch.object(srv._client, "post_json",
                      return_value={"success": False, "errorMsg": "订单已取消，款项原路退回，无需重复退款"}):
        out = json.loads(srv._apply_refund_impl("ORD-1", "x", ctx_user_id="1"))
    assert out["success"] is False and "已取消" in out["message"]


def test_cancel_and_change_address():
    with patch.object(srv._client, "post_json", return_value={"success": True, "data": "订单已取消"}):
        c = json.loads(srv._cancel_order_impl("ORD-1", ctx_user_id="1"))
    assert c["success"] and "取消" in c["message"]
    with patch.object(srv._client, "put_json", return_value={"success": True, "data": "收货地址已更新"}):
        a = json.loads(srv._change_address_impl("ORD-1", "新地址", ctx_user_id="1"))
    assert a["success"] and "地址" in a["message"]


def test_negotiate_price_hides_floor_and_not_below_floor():
    prod = {"success": True, "data": {"id": 1, "title": "鞋", "category": "运动鞋",
            "price": 89900, "floorPrice": 75000, "stock": 10, "sku": "S", "description": "d", "specs": "{}"}}
    with patch.object(srv._client, "get_json", return_value=prod):
        out = json.loads(srv._negotiate_price_impl("1", buyer_offer=600.0, ctx_user_id="1"))
    assert out["success"]
    assert "floor_price" not in out                 # 底价不外泄
    assert out["suggested_price"] >= 750.0          # 永不破底(底价¥750)
    assert out["product_name"] == "鞋"
