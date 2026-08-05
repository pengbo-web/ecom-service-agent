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


# 评价演示数据(N4):对应 mock_data.ORDERS 里四笔已签收订单(1 笔原有 +
# 3 笔为本任务补充,见 mock_data.py 对应注释),评分故意有好有差,好让"评价"
# 控制台的「差评 top 商品」真的摆得出多个商品的对比,而不是一张只有一行的卡片。
# 差评文案(rating<=2)刻意写进至少一个 COMMITMENT_KEYWORDS 词表词
# (见 app/agent/skills/risk.py),这样 review_insights 的词表匹配才有东西可抽——
# 这几款商品的真实名字("Nike Air Max 270 运动鞋"等)全都带型号数字,天生会被
# 数字过滤规则挡在词表外,不能指望"商品名"这条路径命中。
_DEMO_REVIEWS = [
    # (order_id, user, sku, rating, content)
    ("ORD-20240110-003", "大壮", "PHONE-MI14U-BK",
     2, "手机用一周就发热明显,申请退差价客服一直不处理,体验很差"),
    ("ORD-20240105-006", "小明", "SHOE-270-BK-42",
     2, "鞋子穿两天就开胶,申请退款还要自己承担运费,很失望"),
    ("ORD-20240108-007", "小红", "ELEC-APP-002",
     5, "降噪效果很好,续航也够用,物流很快"),
    ("ORD-20240112-008", "阿杰", "HOME-DYSON-V15",
     1, "吸尘器质量有问题,申请全额退但客服一直拖着不处理"),
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
