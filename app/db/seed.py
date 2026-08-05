"""把 mock_data 的内容灌入 SQLite（唯一种子来源，DRY）。"""

import json

from app.db.database import Database


def seed_from_mock(db: Database) -> None:
    from app.agent.tools.mock_data import ORDERS, PRODUCTS, LOGISTICS

    conn = db.connect()
    try:
        for p in PRODUCTS.values():
            conn.execute(
                """INSERT OR REPLACE INTO products
                   (product_id, name, category, price, stock, description, specs, floor_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (p["product_id"], p["name"], p.get("category"), p.get("price"),
                 p.get("stock"), p.get("description", ""),
                 json.dumps(p.get("specs", {}), ensure_ascii=False),
                 p.get("floor_price")),
            )

        users = {}
        for o in ORDERS.values():
            conn.execute(
                """INSERT OR REPLACE INTO orders
                   (order_id, user, status, total, created_at, shipped_at,
                    tracking_number, carrier, estimated_delivery, delivered_at,
                    refund_reason, refund_status, refund_requested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (o["order_id"], o.get("user"), o.get("status"), o.get("total"),
                 o.get("created_at"), o.get("shipped_at"), o.get("tracking_number"),
                 o.get("carrier"), o.get("estimated_delivery"), o.get("delivered_at"),
                 o.get("refund_reason"), o.get("refund_status"),
                 o.get("refund_requested_at")),
            )
            for it in o["items"]:
                conn.execute(
                    """INSERT INTO order_items (order_id, name, sku, quantity, price)
                       VALUES (?, ?, ?, ?, ?)""",
                    (o["order_id"], it["name"], it["sku"], it["quantity"], it["price"]),
                )
            if o.get("user"):
                users[o["user"]] = o["user"]

        for name in users:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, name) VALUES (?, ?)",
                (name, name),
            )

        # 种子会员等级(演示资格过滤:部分用户是会员)
        _LEVELS = {"小明": "diamond", "小红": "gold"}
        for name, lvl in _LEVELS.items():
            if name in users:
                conn.execute("UPDATE users SET member_level = ? WHERE user_id = ?", (lvl, name))

        for lg in LOGISTICS.values():
            conn.execute(
                "INSERT OR REPLACE INTO shipments (tracking_number, carrier, status) VALUES (?, ?, ?)",
                (lg["tracking_number"], lg["carrier"], lg["status"]),
            )
            for seq, ev in enumerate(lg["events"]):
                conn.execute(
                    """INSERT INTO logistics_events
                       (tracking_number, seq, time, location, description)
                       VALUES (?, ?, ?, ?, ?)""",
                    (lg["tracking_number"], seq, ev["time"], ev["location"], ev["description"]),
                )
        conn.commit()
    finally:
        conn.close()

    _seed_reviews(db)


# 评价演示数据(N4):只对 mock_data.ORDERS 里**真正已签收**的订单项造评价——
# 目前只有 ORD-20240110-003(大壮 / 小米14 Ultra 手机)一笔是 delivered。
# 刻意不在这里另造合成订单来凑"多条评价":test_db_repository.py::
# test_list_orders_count、test_tools_db.py::test_list_user_orders 都断言过
# `len(db.list_orders()) == len(mock_data.ORDERS)`——种子脚本擅自插入
# mock_data 之外的订单会让这两个既有断言失真,而这两个测试文件不在本任务
# 允许改动的文件范围内。因此这里只种一条评价:避免控制台在全新安装时是
# 空的,但不假装有更多"已签收"库存来演示多商品对比。
_DEMO_REVIEWS = [
    # (order_id, user, sku, rating, content)
    ("ORD-20240110-003", "大壮", "PHONE-MI14U-BK",
     2, "手机发热比较明显,信号也不太稳定,客服回复也慢"),
]


def _seed_reviews(db: Database) -> None:
    """种子评价演示数据,避免"评价"控制台在全新安装时是空的。

    走 `Database.create_review` 落评价——复用与买家提交同一条校验路径(只有
    delivered 订单、且该订单确实属于这个用户才能评),种子数据本身也不能
    绕开这条规则。`create_review` 自带的 UNIQUE 判重意味着重复跑本函数
    (如多次执行种子脚本)不会插出重复评价,是安全的幂等操作。
    """
    for order_id, user, sku, rating, content in _DEMO_REVIEWS:
        db.create_review(order_id, user, sku, rating, content)
