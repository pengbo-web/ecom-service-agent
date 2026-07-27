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
