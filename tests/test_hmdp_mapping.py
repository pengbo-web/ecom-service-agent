"""hmdp DTO ↔ agent 数据契约 的纯函数映射(分→元、字段改名、剔除底价)。"""

from mcp_server.hmdp_mapping import map_product, map_order, map_logistics


def test_map_product_fen_to_yuan_and_hide_floor():
    hp = {"id": 2, "title": "小米14 Ultra 手机", "category": "手机",
          "price": 599900, "floorPrice": 560000, "stock": 42,
          "sku": "PHONE-MI14U-BK", "description": "骁龙8", "specs": '{"颜色":"黑色"}'}
    pub = map_product(hp, public=True)
    assert pub["product_id"] == "2"
    assert pub["name"] == "小米14 Ultra 手机"
    assert pub["price"] == 5999.0            # 分→元
    assert pub["specs"] == {"颜色": "黑色"}    # JSON 串→dict
    assert "floor_price" not in pub          # 公开视图剔除底价
    internal = map_product(hp, public=False)
    assert internal["floor_price"] == 5600.0  # 内部保留(议价用)


def test_map_product_null_floor_and_bad_specs():
    hp = {"id": 3, "title": "耳机", "category": "耳机", "price": 179900,
          "floorPrice": None, "stock": 5, "sku": "X", "description": None, "specs": None}
    pub = map_product(hp, public=True)
    assert pub["price"] == 1799.0
    assert pub["specs"] == {}                 # None specs → {}
    assert pub["description"] == ""            # None → ""


def test_map_order_fields_and_items():
    ho = {"order_no": "ORD-20240115-001", "user_id": 1, "status": "shipped",
          "total": 89900, "shipping_address": "上海", "tracking_number": "SF1",
          "carrier": "顺丰", "items": [{"name": "鞋", "sku": "S1", "quantity": 1, "price": 89900}]}
    o = map_order(ho)
    assert o["order_id"] == "ORD-20240115-001"
    assert o["user"] == "1"                    # 归属字段=字符串 userId
    assert o["status"] == "shipped"
    assert o["total"] == 899.0
    assert o["items"][0]["price"] == 899.0     # item 价也分→元
    assert o["tracking_number"] == "SF1"


def test_map_logistics_passthrough():
    hl = {"tracking_number": "SF1", "carrier": "顺丰", "status": "in_transit",
          "events": [{"time": "t", "location": "l", "description": "d"}]}
    lg = map_logistics(hl)
    assert lg["tracking_number"] == "SF1"
    assert lg["events"][0]["description"] == "d"
