# 订单归属校验(修跨用户越权) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 修复"拿到别人订单号即可查详情/物流、退款、改地址、取消、催发、开发票,且 `list_user_orders` 返回全量订单"的跨用户越权——所有订单工具改走统一归属校验:auth 开启时**订单不属于当前登录用户即视同不存在**(隐私 fail-closed),auth 关闭时放行(教学单机保持现状)。

**Architecture:** 7 个单订单工具都通过同一入口 `get_db().get_order(order_id)` 取单——加一个 `owned_order(order_id)` 归属校验 helper 替换它,越权返回 `None`,各工具已有的"order is None → 未找到订单"逻辑天然复用(不泄露订单是否存在)。`list_user_orders` 按当前用户过滤。身份复用优惠券改造已建的 `runtime_context.get_current_user()`(chat 每轮刷新);受 `auth_enabled` 门控(与 U3 身份强制一致)。

**Tech Stack:** 现有 tools/database/runtime_context,标准库。零新依赖。

## Global Constraints

- **归属校验 = auth 门控 + 隐私 fail-closed(逐字)**:`owned_order(order_id)` —— 订单不存在→`None`;`auth_enabled=False`→放行(返回订单,教学现状);`auth_enabled=True` 且 `get_current_user()` 为空→`None`(拒,订单是隐私,**与 query_coupons 的 fail-open 相反**);`auth_enabled=True` 且订单 `user != 当前用户`→`None`(越权视同不存在,不泄露存在性);属于→返回订单。
- **不改 7 个工具的后续逻辑**:只把取单调用 `db.get_order(order_id)` / `get_db().get_order(order_id)` 换成 `owned_order(order_id)`;各工具已有 `if order is None: return {"success": False, "error": "未找到订单..."}` 分支复用——越权与不存在返回同样话术。
- **`list_user_orders` 过滤(逐字)**:`auth_enabled=False`→全量(现状);`auth_enabled=True`→只返回 `user == get_current_user()` 的订单,`get_current_user()` 为空→空列表(隐私)。
- **越权不泄露**:被拒一律走"未找到该订单"话术,不能区分"订单不存在"与"订单存在但不是你的"。
- **order dict 的归属字段是 `user`**(orders 表列名 `user`,`get_order` 返回 `dict(row)` 含之)。
- **current_user 复用**:`app/agent/runtime_context.get_current_user()`(券改造已建,chat 每轮 `set_current_user`);本方案不重复建。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 origin,绝不 upstream;`.venv/Scripts/python.exe`;测试不 print emoji。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/agent/tools/ownership.py`(新) | O1 | `owned_order(order_id)` 归属校验(auth 门控 + fail-closed) |
| `app/agent/tools/order.py` / `logistics.py` / `refund.py` / `order_ops.py`(改) | O2 | 7 工具 get_order→owned_order |
| `app/agent/tools/user_orders.py`(改) | O2 | `list_user_orders` 按当前用户过滤 |
| `tests/test_order_ownership.py`(新) | O1/O2 | helper 各分支 + 每类工具越权拒绝 + list 过滤 |

---

### Task O1: owned_order 归属校验 helper

**Files:**
- Create: `app/agent/tools/ownership.py`
- Test: `tests/test_order_ownership.py`(新)

**Interfaces:**
- Consumes: `get_db().get_order(order_id)`(返回含 `user`);`runtime_context.get_current_user()`;`settings.auth_enabled`。
- Produces: `owned_order(order_id: str) -> dict | None`(O2 全部工具依赖)。

- [ ] **Step 1: 写失败测试** `tests/test_order_ownership.py`

```python
"""订单归属校验:auth 门控 + 隐私 fail-closed。临时库,全离线。"""

import sqlite3
import pytest

from app.agent.runtime_context import set_current_user
from app.agent.tools.ownership import owned_order
from app.config.settings import settings
from app.db import Database, set_db


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db")); d.init_schema()
    conn = sqlite3.connect(d.db_path)
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O-A','alice','pending',10,'t')")
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O-B','bob','shipped',20,'t')")
    conn.commit(); conn.close()
    set_db(d)
    yield d
    set_db(None); set_current_user(None)


def test_missing_order_returns_none(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert owned_order("O-NONE") is None


def test_auth_off_passes_through(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    set_current_user(None)
    assert owned_order("O-B")["order_id"] == "O-B"   # 教学放行,不校验


def test_owner_gets_order(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert owned_order("O-A")["order_id"] == "O-A"


def test_foreign_order_denied_as_none(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert owned_order("O-B") is None                # bob 的单,alice 越权→None


def test_auth_on_no_user_denied(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user(None)
    assert owned_order("O-A") is None                # 隐私 fail-closed
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_order_ownership.py -q` FAIL

- [ ] **Step 3: 实现** `app/agent/tools/ownership.py`

```python
"""订单归属校验:防跨用户越权(拿别人订单号查/改/退)。

所有按 order_id 操作的工具经此取单——越权视同订单不存在(返回 None),
调用方已有的"未找到订单"话术复用,不泄露订单存在性。

门控与隐私策略:
- auth_enabled=False:教学单机,放行(不校验,保持现状)。
- auth_enabled=True:订单必须属于当前登录用户;拿不到当前用户 → 拒
  (fail-closed,订单是隐私,与优惠券 query_coupons 的 fail-open 相反)。
"""

from __future__ import annotations


def owned_order(order_id: str):
    """取单并做归属校验;不存在/越权/无身份(auth 开)→ None。"""
    from app.db import get_db
    order = get_db().get_order(order_id)
    if order is None:
        return None
    from app.config.settings import settings
    if not settings.auth_enabled:
        return order
    from app.agent.runtime_context import get_current_user
    uid = get_current_user()
    if not uid:
        return None
    return order if order.get("user") == uid else None
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_order_ownership.py -q` PASS
- [ ] **Step 5: 提交** `feat(tools): O1 owned_order 订单归属校验 helper(auth 门控+fail-closed)`

---

### Task O2: 接入 7 工具 + list_user_orders 过滤

**Files:**
- Modify: `app/agent/tools/order.py`(query_order)、`logistics.py`(query_logistics)、`refund.py`(apply_refund)、`order_ops.py`(change_address/cancel_order/expedite_shipping/issue_invoice)、`user_orders.py`(list_user_orders)
- Test: `tests/test_order_ownership.py`(增)

**Interfaces:**
- Consumes: O1 的 `owned_order`;`get_current_user`;`settings.auth_enabled`;`get_db().list_orders()`。
- Produces: 8 个工具的越权隔离(auth 开)/ 现状(auth 关)。

- [ ] **Step 1: 写失败测试**(增到 `tests/test_order_ownership.py`;沿用上面的 db fixture,补 order_items 让 query_order 完整)

```python
def test_query_order_blocks_foreign(db, monkeypatch):
    from app.agent.tools.order import query_order
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    r = query_order("O-B")                            # bob 的单
    assert r["success"] is False                      # 视同未找到
    r2 = query_order("O-A")                            # 自己的
    assert r2["success"] is True


def test_apply_refund_blocks_foreign(db, monkeypatch):
    from app.agent.tools.refund import apply_refund
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert apply_refund("O-B", "不想要了")["success"] is False   # 越权退款被拒


def test_change_address_blocks_foreign(db, monkeypatch):
    from app.agent.tools.order_ops import change_address
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert change_address("O-B", "新地址")["success"] is False


def test_query_logistics_blocks_foreign(db, monkeypatch):
    from app.agent.tools.logistics import query_logistics
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert query_logistics("O-B")["success"] is False


def test_list_user_orders_filters_by_current_user(db, monkeypatch):
    from app.agent.tools.user_orders import list_user_orders
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    r = list_user_orders()
    ids = {o["order_id"] for o in r["orders"]}
    assert ids == {"O-A"} and r["count"] == 1          # 只见自己的


def test_list_user_orders_auth_off_returns_all(db, monkeypatch):
    from app.agent.tools.user_orders import list_user_orders
    monkeypatch.setattr(settings, "auth_enabled", False)
    set_current_user(None)
    r = list_user_orders()
    assert r["count"] == 2                              # 教学放行,全量


def test_auth_on_no_user_list_empty(db, monkeypatch):
    from app.agent.tools.user_orders import list_user_orders
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user(None)
    assert list_user_orders()["count"] == 0            # 隐私 fail-closed
```

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现**——7 个单订单工具:把取单行替换(其余不动)。逐文件:

`order.py` query_order:`order = get_db().get_order(order_id)` → 顶部 `from app.agent.tools.ownership import owned_order`,行改 `order = owned_order(order_id)`。
`logistics.py` query_logistics:`order = db.get_order(order_id)` → `order = owned_order(order_id)`(import 同上;`db` 若仅此一处用可保留其它调用)。
`refund.py` apply_refund:`order = db.get_order(order_id)` → `owned_order(order_id)`。
`order_ops.py` change_address/cancel_order/expedite_shipping/issue_invoice:四处 `order = db.get_order(order_id)` → `owned_order(order_id)`(文件顶部加一次 import)。

（每处后续 `if order is None: return {"success": False, "error": "未找到订单 ..."}` 已存在,不改；越权即走该分支。）

`user_orders.py` list_user_orders:

```python
def list_user_orders() -> dict:
    """查询【当前用户】的订单概要列表(auth 开时按登录身份隔离)。"""
    from app.config.settings import settings
    raw = get_db().list_orders()
    if settings.auth_enabled:
        from app.agent.runtime_context import get_current_user
        uid = get_current_user()
        raw = [o for o in raw if uid and o.get("user") == uid]
    orders = [
        {
            "order_id": o["order_id"],
            "status": STATUS_LABELS.get(o["status"], o["status"]),
            "items_summary": "、".join(item["name"] for item in o["items"]),
            "total": o["total"],
            "created_at": o["created_at"],
        }
        for o in raw
    ]
    return {"success": True, "count": len(orders), "orders": orders}
```

- [ ] **Step 4: 跑通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_order_ownership.py tests/test_order_ops.py tests/test_tools_db.py tests/test_coupon_eligibility.py tests/test_react_degrade.py -q`
Expected: PASS(既有 order_ops/tools_db 测试若不设 current_user 且未开 auth——conftest 已强制 `auth_enabled=False`,天然走放行分支,行为不变;若某测试显式开了 auth 又查订单需补 set_current_user,本任务范围内适配)

- [ ] **Step 5: 提交** `feat(tools): O2 订单工具接入归属校验 + list_user_orders 按用户过滤`

---

### Task O3: 端到端冒烟(控制方执行)

- [ ] 重启服务(auth 开)→ 登录 `大壮`(种子用户,有 1 单)→ 问"我有哪些订单" → 只见**大壮自己的**订单(不再是全量 5 单)
- [ ] 大壮尝试查/退别人的订单号(如 `ORD-20240115-001` 若不属于大壮)→ "未找到该订单"(越权被拒,不泄露存在性)
- [ ] 登录新用户 `pengbo`(无订单)→ "我有哪些订单" → 空(而非全量)
- [ ] `.env` 临时 `AUTH_ENABLED=false` 重启 → 查订单回到全量(教学现状保留);恢复
- [ ] Langfuse 该轮:`execute_tool list_user_orders` output 已按用户收窄
- [ ] `.superpowers/sdd/progress.md` 记账;此项修复填补了优惠券方案里记的"相关发现"

## 总量与顺序

O1(~0.3d)→ O2(~0.4d)→ O3(~0.1d),共 **~0.8 人日**。

## Self-Review

- **覆盖核对**:owned_order 五分支(不存在/auth关/属于/越权/无身份)O1 测试全覆盖 ✅;7 单订单工具接入(query_order/logistics/refund/change_address/cancel/expedite/invoice)O2 代表性测试(query/refund/change/logistics)+ 全部改同一行 ✅;list_user_orders 三态(过滤/auth关全量/无身份空)✅;越权不泄露(返回同"未找到")✅;fail-closed 与券的 fail-open 对比写明 ✅;auth 关教学现状保留 ✅。
- **占位符扫描**:O2 对 cancel/expedite/invoice 未单列测试但明确"全部改同一行 + 代表性 4 工具测试"——同源改动,代表性覆盖足够(非 TBD);无占位。
- **类型一致性**:`owned_order(order_id) -> dict | None` O1 定义、O2 全工具消费一致;`get_current_user`/`settings.auth_enabled`/`order["user"]` 贯穿;list_user_orders 返回结构 `{success,count,orders}` 未变(只收窄 raw)。
- **已知取舍**:①order_id 可枚举(ORD-日期-序号)本可加更强防护,但归属校验已挡住"看到别人订单内容",枚举只能探测存在性且已被"未找到"话术模糊,足够;②query_order 等的越权返回与"不存在"同话术,牺牲一点用户友好换不泄露(安全优先);③auth 关时完全放行——单机教学定位,文档已在 auth 方案标注"生产 auth 必开"。
