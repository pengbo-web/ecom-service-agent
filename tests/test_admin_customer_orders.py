"""坐席侧读客户订单。

补的是坐席台上最刺眼的一块空白:**接待人看不到客户买了什么**。改造前右侧客户
面板只有会话 ID / 轮次 / 最后活跃这类会话元数据,而买家开口第一句几乎总是关于
某一笔订单——坐席只能反问"您的订单号是多少",把 AI 已经知道的事情重新问一遍人。
"""

from __future__ import annotations

import inspect

from app.api import app as app_module


def test_endpoint_requires_admin_auth():
    """跨用户读订单必须是 admin 权限,买家 token 拿不到别人的订单。"""
    src = inspect.getsource(app_module)
    idx = src.index("/api/admin/customer/{user_id}/orders")
    decorator = src[idx - 200:idx + 200]
    assert "admin_auth" in decorator


def test_falls_back_to_local_db_even_when_hmdp_fails():
    """hmdp 失败时**也要**回落本地订单库。

    这是我自己第一版写错的地方:当时写成"只有在没报错且为空时才回落",于是
    hmdp 一超时,坐席面板就是一片空白——恰恰是这个端点要消灭的那个状态。
    坐席宁可看到一份可能过时的订单(并被明确告知),也好过面对空白去问买家。
    """
    src = inspect.getsource(app_module)
    body = src[src.index("def admin_customer_orders"):]
    body = body[:body.index("@app.get(\"/api/handoffs\"")]
    assert "if not orders:" in body, "回退条件不能带 `and not degraded`"


def test_reuses_the_same_read_path_as_buyer_orders():
    """与买家自己的 /api/orders 共用同一条读取路径。

    两边看到的必须是同一份事实,否则坐席据以答复的内容和买家屏幕上显示的对不上,
    比看不到更糟。所以复用 `_hmdp_my_orders` / `list_orders` + `_fmt_order`,
    而不是另写一份查询。
    """
    src = inspect.getsource(app_module)
    body = src[src.index("def admin_customer_orders"):]
    body = body[:body.index("@app.get(\"/api/handoffs\"")]
    assert "_hmdp_my_orders" in body
    assert "_fmt_order" in body


def test_distinguishes_empty_from_degraded():
    """空列表有两种含义,必须分开下发。

    把"订单服务连不上"显示成"该客户暂无订单",坐席会据此对买家说
    "您名下没有订单"——那是一句由前端渲染逻辑造出来的假话。
    """
    src = inspect.getsource(app_module)
    body = src[src.index("def admin_customer_orders"):]
    body = body[:body.index("@app.get(\"/api/handoffs\"")]
    assert '"degraded": degraded' in body
    assert '"success": not degraded' in body
