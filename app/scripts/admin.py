"""后台改数据 CLI：
    python -m app.scripts.admin set-status <order_id> <status>
    python -m app.scripts.admin set-stock <product_id> <stock>
"""

import sys

from app.db import get_db


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 3:
        print(__doc__)
        return 1

    cmd, key, value = argv
    db = get_db()
    if cmd == "set-status":
        ok = db.update_order_status(key, value)
        print(f"{'成功' if ok else '失败(订单不存在)'}: 订单 {key} 状态 → {value}")
    elif cmd == "set-stock":
        ok = db.update_stock(key, int(value))
        print(f"{'成功' if ok else '失败(商品不存在)'}: 商品 {key} 库存 → {value}")
    else:
        print(__doc__)
        return 1
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
