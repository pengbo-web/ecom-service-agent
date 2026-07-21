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
