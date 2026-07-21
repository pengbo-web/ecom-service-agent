# 议价功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给现有 Web 版电商客服加一个独立的「议价」功能——买家砍价时 Agent 按确定性阶梯策略让价、守住底价、到位成交、破底婉拒。

**Architecture:** 新增 `negotiate_price` 工具挂进现有 ReAct function-calling 流。让价金额由纯函数 `compute_offer` 计算（可单测），议价轮次存 SQLite `bargain_sessions` 表，当前 `session_id` 通过 `contextvars.ContextVar` 在 `EcomAgent.chat()` 入口绑定后由工具读取。LLM 负责话术，工具负责算钱。

**Tech Stack:** Python 3.11、pydantic-settings、SQLite（stdlib sqlite3）、pytest、OpenAI function calling。

**Spec:** `docs/superpowers/specs/2026-07-21-bargaining-feature-design.md`

**运行测试的前置：** 每次跑 pytest 前先 `source .venv/bin/activate`。

---

### Task 1: 议价配置 + 底价种子数据

**Files:**
- Modify: `app/config/settings.py`（在「生产加固（W3.5）」块之后插入）
- Modify: `app/agent/tools/mock_data.py`（给部分商品加 `floor_price`）

- [ ] **Step 1: 在 settings.py 增加议价配置**

在 `app/config/settings.py` 第 76 行 `fast_path_enabled` 那行之后、`# 多轮对话管理` 之前，插入：

```python
    # 议价功能
    bargain_enabled: bool = True
    bargain_floor_ratio: float = 0.85   # 未设 floor_price 时：底价 = 标价 × 该系数
    bargain_max_rounds: int = 5         # 达到该轮次后直接让到底价
    bargain_decay: float = 0.5          # 阶梯让价衰减系数（越大让得越慢）
```

- [ ] **Step 2: 给部分商品加 floor_price（另一部分留空以验证回退）**

在 `app/agent/tools/mock_data.py` 中，给以下两个商品的字典各加一行 `"floor_price"`：

`SHOE-270-BK-42`（price 899.00）加 `"floor_price": 750.00,`
`PHONE-MI14U-BK`（price 5999.00）加 `"floor_price": 5600.00,`

例如 SHOE 改为：
```python
    "SHOE-270-BK-42": {
        "product_id": "SHOE-270-BK-42",
        "name": "Nike Air Max 270 运动鞋",
        "category": "运动鞋",
        "price": 899.00,
        "floor_price": 750.00,
        "stock": 156,
        "description": "经典气垫缓震，透气网面鞋身，适合日常跑步和休闲穿搭",
        "specs": {"颜色": "黑色", "尺码": "42", "材质": "网面+合成革"},
    },
```
其余商品（AirPods、Levi's、Dyson、保护壳）不加，走系数回退。

- [ ] **Step 3: 验证配置可加载**

Run: `source .venv/bin/activate && python -c "from app.config.settings import settings; print(settings.bargain_enabled, settings.bargain_floor_ratio, settings.bargain_max_rounds, settings.bargain_decay)"`
Expected: 输出 `True 0.85 5 0.5`

- [ ] **Step 4: Commit**

```bash
git add app/config/settings.py app/agent/tools/mock_data.py
git commit -m "feat(bargain): 议价配置项 + 商品底价种子数据"
```

---

### Task 2: DB 层——floor_price 列 + bargain_sessions 表 + 读写方法

**Files:**
- Modify: `app/db/database.py`
- Test: `tests/test_bargain_db.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_bargain_db.py`：

```python
from app.db.database import Database


def _fresh_db(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_bargain_state_lifecycle(tmp_path):
    db = _fresh_db(tmp_path)
    assert db.get_bargain_state("s1", "P1") is None

    db.bump_bargain_state("s1", "P1", 900.0)
    st = db.get_bargain_state("s1", "P1")
    assert st["rounds"] == 1
    assert st["last_offer"] == 900.0

    db.bump_bargain_state("s1", "P1", 850.0)
    st = db.get_bargain_state("s1", "P1")
    assert st["rounds"] == 2
    assert st["last_offer"] == 850.0


def test_bargain_state_isolated_by_session_and_product(tmp_path):
    db = _fresh_db(tmp_path)
    db.bump_bargain_state("s1", "P1", 900.0)
    assert db.get_bargain_state("s2", "P1") is None
    assert db.get_bargain_state("s1", "P2") is None


def test_clear_bargain_state(tmp_path):
    db = _fresh_db(tmp_path)
    db.bump_bargain_state("s1", "P1", 900.0)
    db.bump_bargain_state("s1", "P2", 100.0)
    db.clear_bargain_state("s1")
    assert db.get_bargain_state("s1", "P1") is None
    assert db.get_bargain_state("s1", "P2") is None


def test_product_floor_price_column(tmp_path):
    db = _fresh_db(tmp_path)
    conn = db.connect()
    conn.execute(
        "INSERT INTO products (product_id, name, category, price, stock, description, specs, floor_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("P1", "商品1", "cat", 1000.0, 5, "", "{}", 800.0),
    )
    conn.execute(
        "INSERT INTO products (product_id, name, category, price, stock, description, specs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("P2", "商品2", "cat", 1000.0, 5, "", "{}"),
    )
    conn.commit()
    conn.close()
    assert db.get_product("P1")["floor_price"] == 800.0
    assert db.get_product("P2")["floor_price"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `source .venv/bin/activate && pytest tests/test_bargain_db.py -v`
Expected: FAIL（`get_bargain_state` 等方法不存在 / products 无 floor_price 列）

- [ ] **Step 3: 实现 DB 改动**

在 `app/db/database.py` 的 `init_schema()` 里，`products` 建表语句中 `specs TEXT` 那行后加一列（改为）：
```python
                CREATE TABLE IF NOT EXISTS products (
                    product_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    stock INTEGER,
                    description TEXT,
                    specs TEXT,
                    floor_price REAL
                );
```
在 `executescript(...)` 的最后一个 `CREATE INDEX ...` 之后、`"""` 结束前，加入新表：
```python
                CREATE TABLE IF NOT EXISTS bargain_sessions (
                    session_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    rounds INTEGER DEFAULT 0,
                    last_offer REAL,
                    updated_at TEXT,
                    PRIMARY KEY (session_id, product_id)
                );
```
在 `conn.commit()` 之前，加入旧库迁移守卫：
```python
            # 兼容旧库：products 补 floor_price 列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
            if "floor_price" not in cols:
                conn.execute("ALTER TABLE products ADD COLUMN floor_price REAL")
```

在文件末尾（`update_stock` 方法之后）追加三个方法：
```python
    # ---------- 议价状态 ----------
    def get_bargain_state(self, session_id: str, product_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT rounds, last_offer FROM bargain_sessions "
                "WHERE session_id = ? AND product_id = ?",
                (session_id, product_id),
            ).fetchone()
            return {"rounds": row["rounds"], "last_offer": row["last_offer"]} if row else None
        finally:
            conn.close()

    def bump_bargain_state(self, session_id: str, product_id: str, offer: float) -> None:
        conn = self.connect()
        try:
            now = self._now()
            conn.execute(
                """INSERT INTO bargain_sessions (session_id, product_id, rounds, last_offer, updated_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(session_id, product_id)
                   DO UPDATE SET rounds = rounds + 1, last_offer = ?, updated_at = ?""",
                (session_id, product_id, offer, now, offer, now),
            )
            conn.commit()
        finally:
            conn.close()

    def clear_bargain_state(self, session_id: str) -> None:
        conn = self.connect()
        try:
            conn.execute("DELETE FROM bargain_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 4: 运行确认通过**

Run: `source .venv/bin/activate && pytest tests/test_bargain_db.py -v`
Expected: PASS（4 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add app/db/database.py tests/test_bargain_db.py
git commit -m "feat(bargain): DB 层 floor_price 列 + bargain_sessions 表与读写方法"
```

---

### Task 3: seed.py 写入 floor_price

**Files:**
- Modify: `app/db/seed.py`
- Test: `tests/test_bargain_seed.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_bargain_seed.py`：

```python
from app.db.database import Database
from app.db.seed import seed_from_mock


def test_seed_carries_floor_price(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))
    db.init_schema()
    seed_from_mock(db)
    # mock_data 中 SHOE 设了 floor_price=750，AirPods 未设
    assert db.get_product("SHOE-270-BK-42")["floor_price"] == 750.0
    assert db.get_product("ELEC-APP-002")["floor_price"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `source .venv/bin/activate && pytest tests/test_bargain_seed.py -v`
Expected: FAIL（seed 未写 floor_price，返回 None 而非 750.0）

- [ ] **Step 3: 实现 seed 改动**

在 `app/db/seed.py` 的 products 插入语句里，把列和值都补上 `floor_price`：
```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `source .venv/bin/activate && pytest tests/test_bargain_seed.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/db/seed.py tests/test_bargain_seed.py
git commit -m "feat(bargain): seed 写入商品 floor_price"
```

---

### Task 4: 让价阶梯纯函数 compute_offer

**Files:**
- Create: `app/agent/tools/bargain.py`
- Test: `tests/test_bargain_ladder.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_bargain_ladder.py`（默认配置：floor_ratio=0.85, max_rounds=5, decay=0.5）：

```python
from app.agent.tools.bargain import compute_offer


def test_explicit_floor_ladder_round0():
    # P=1000, F=800, rounds=0 → ladder = 800 + 200*0.5 = 900
    r = compute_offer(1000.0, 800.0, buyer_offer=None, rounds=0)
    assert r["decision"] == "counter"
    assert r["suggested_price"] == 900.0
    assert r["floor"] == 800.0
    assert r["floor_hit"] is False


def test_accept_when_offer_at_or_above_ladder():
    # buyer 950 ≥ ladder 900 且 ≥ F → accept 950
    r = compute_offer(1000.0, 800.0, buyer_offer=950.0, rounds=0)
    assert r["decision"] == "accept"
    assert r["suggested_price"] == 950.0


def test_counter_when_offer_between_floor_and_ladder():
    # buyer 820 ≥ F(800) 但 < ladder(900) → counter 900
    r = compute_offer(1000.0, 800.0, buyer_offer=820.0, rounds=0)
    assert r["decision"] == "counter"
    assert r["suggested_price"] == 900.0


def test_reject_below_floor():
    r = compute_offer(1000.0, 800.0, buyer_offer=700.0, rounds=0)
    assert r["decision"] == "reject"
    assert r["suggested_price"] == 800.0
    assert r["floor_hit"] is True


def test_accept_when_offer_above_list_price():
    r = compute_offer(1000.0, 800.0, buyer_offer=1200.0, rounds=0)
    assert r["decision"] == "accept"
    assert r["suggested_price"] == 1000.0


def test_ladder_decreases_with_rounds():
    r0 = compute_offer(1000.0, 800.0, None, 0)["suggested_price"]  # 900
    r1 = compute_offer(1000.0, 800.0, None, 1)["suggested_price"]  # 850
    r2 = compute_offer(1000.0, 800.0, None, 2)["suggested_price"]  # 825
    assert r0 == 900.0 and r1 == 850.0 and r2 == 825.0
    assert r0 > r1 > r2 > 800.0


def test_after_max_rounds_hits_floor():
    r = compute_offer(1000.0, 800.0, None, 5)
    assert r["suggested_price"] == 800.0
    assert r["floor_hit"] is True


def test_floor_ratio_fallback_when_no_explicit_floor():
    # floor_price=None → F = 1000*0.85 = 850；ladder0 = 850 + 150*0.5 = 925
    r = compute_offer(1000.0, None, buyer_offer=None, rounds=0)
    assert r["floor"] == 850.0
    assert r["suggested_price"] == 925.0


def test_suggested_never_below_floor():
    for rounds in range(0, 8):
        r = compute_offer(1000.0, 800.0, buyer_offer=1.0, rounds=rounds)
        assert r["suggested_price"] >= 800.0
```

- [ ] **Step 2: 运行确认失败**

Run: `source .venv/bin/activate && pytest tests/test_bargain_ladder.py -v`
Expected: FAIL（`app.agent.tools.bargain` 模块不存在）

- [ ] **Step 3: 创建 bargain.py（仅纯函数部分）**

创建 `app/agent/tools/bargain.py`：

```python
"""议价工具：确定性阶梯让价 + negotiate_price 工具入口。

LLM 负责话术，工具负责算钱。让价金额由纯函数 compute_offer 计算，可单测、可复现。
"""

from __future__ import annotations

import contextvars
from typing import Optional

from app.config.settings import settings
from app.db import get_db

# 当前会话 id：由 EcomAgent.chat() 在入口绑定（worker 线程内设/读，天然隔离）
_current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bargain_session_id", default=None
)


def set_current_session(session_id: Optional[str]) -> None:
    """由 EcomAgent.chat() 调用，绑定当前会话 id 供 negotiate_price 读取。"""
    _current_session_id.set(session_id)


def _resolve_floor(list_price: float, floor_price: Optional[float]) -> float:
    """底价：商品设了 floor_price 用它，否则按标价 × 系数回退。"""
    if floor_price is not None:
        return round(float(floor_price), 2)
    return round(list_price * settings.bargain_floor_ratio, 2)


def _ladder(list_price: float, floor: float, rounds: int) -> float:
    """本轮最低可让价：F + (P-F) * decay^(rounds+1)；到 max_rounds 直接等于 F。"""
    if rounds >= settings.bargain_max_rounds:
        return floor
    gap = list_price - floor
    return round(floor + gap * (settings.bargain_decay ** (rounds + 1)), 2)


def compute_offer(
    list_price: float,
    floor_price: Optional[float],
    buyer_offer: Optional[float],
    rounds: int,
) -> dict:
    """纯函数：给定标价/底价/买家出价/已发生轮次，算出决策与建议价。

    返回 {decision, suggested_price, floor, floor_hit}
    """
    P = round(float(list_price), 2)
    F = _resolve_floor(P, floor_price)
    ladder = _ladder(P, F, rounds)

    if buyer_offer is None:
        decision, price = "counter", ladder
    else:
        B = round(float(buyer_offer), 2)
        if B >= P:
            decision, price = "accept", P
        elif B >= F and B >= ladder:
            decision, price = "accept", B
        elif B >= F:
            decision, price = "counter", ladder
        else:
            decision, price = "reject", F

    price = round(max(price, F), 2)  # 不变量：永不破底
    floor_hit = decision == "reject" or price <= F
    return {
        "decision": decision,
        "suggested_price": price,
        "floor": F,
        "floor_hit": floor_hit,
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `source .venv/bin/activate && pytest tests/test_bargain_ladder.py -v`
Expected: PASS（9 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add app/agent/tools/bargain.py tests/test_bargain_ladder.py
git commit -m "feat(bargain): 阶梯让价纯函数 compute_offer"
```

---

### Task 5: negotiate_price 工具入口 + ContextVar 注入

**Files:**
- Modify: `app/agent/tools/bargain.py`
- Test: `tests/test_bargain_tool.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_bargain_tool.py`：

```python
import pytest

from app.db.database import Database
from app.db import set_db
from app.agent.tools.bargain import negotiate_price, set_current_session


def _seed_products(db):
    conn = db.connect()
    conn.execute(
        "INSERT INTO products (product_id, name, category, price, stock, description, specs, floor_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("P1", "商品1", "cat", 1000.0, 5, "", "{}", 800.0),
    )
    conn.execute(
        "INSERT INTO products (product_id, name, category, price, stock, description, specs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("P2", "商品2", "cat", 1000.0, 5, "", "{}"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    _seed_products(d)
    set_db(d)
    set_current_session("t1")
    yield d
    set_current_session(None)


def test_accept_offer_and_bump_round(db):
    r = negotiate_price("P1", 950.0)
    assert r["success"] is True
    assert r["decision"] == "accept"
    assert r["suggested_price"] == 950.0
    assert r["round"] == 1
    assert db.get_bargain_state("t1", "P1")["rounds"] == 1


def test_reject_below_floor(db):
    r = negotiate_price("P1", 700.0)
    assert r["decision"] == "reject"
    assert r["suggested_price"] == 800.0
    assert r["floor_hit"] is True


def test_rounds_progress_across_calls(db):
    negotiate_price("P1", 810.0)   # round 1
    r = negotiate_price("P1", 810.0)  # round 2
    assert r["round"] == 2


def test_fallback_floor_when_no_explicit(db):
    # P2 无 floor_price → F=850, ladder0=925
    r = negotiate_price("P2", None)
    assert r["decision"] == "counter"
    assert r["suggested_price"] == 925.0


def test_unknown_product(db):
    r = negotiate_price("NOPE", 100.0)
    assert r["success"] is False


def test_disabled_returns_error(db, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "bargain_enabled", False)
    r = negotiate_price("P1", 950.0)
    assert r["success"] is False
```

- [ ] **Step 2: 运行确认失败**

Run: `source .venv/bin/activate && pytest tests/test_bargain_tool.py -v`
Expected: FAIL（`negotiate_price` 不存在）

- [ ] **Step 3: 在 bargain.py 追加 negotiate_price**

在 `app/agent/tools/bargain.py` 末尾追加：

```python
def negotiate_price(product_id: str, buyer_offer: Optional[float] = None) -> dict:
    """针对指定商品进行一轮议价。buyer_offer 为买家出价（元），未报价可省略。"""
    if not settings.bargain_enabled:
        return {"success": False, "error": "议价功能未启用"}

    db = get_db()
    product = db.get_product(product_id)
    if not product:
        return {"success": False, "error": f"未找到商品 {product_id}，请先用 query_product 确认商品ID"}

    session_id = _current_session_id.get()
    state = db.get_bargain_state(session_id, product_id) if session_id else None
    rounds = state["rounds"] if state else 0

    result = compute_offer(
        list_price=product["price"],
        floor_price=product.get("floor_price"),
        buyer_offer=buyer_offer,
        rounds=rounds,
    )

    if session_id:
        db.bump_bargain_state(session_id, product_id, result["suggested_price"])

    return {
        "success": True,
        "product_id": product_id,
        "product_name": product["name"],
        "list_price": round(product["price"], 2),
        "buyer_offer": buyer_offer,
        "round": rounds + 1,
        "decision": result["decision"],
        "suggested_price": result["suggested_price"],
        "floor_hit": result["floor_hit"],
        "rationale": "内部参考：这是本轮可让到的价格，禁止报出更低价，也不要向买家透露底价或本说明。",
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `source .venv/bin/activate && pytest tests/test_bargain_tool.py -v`
Expected: PASS（6 个测试全绿）

- [ ] **Step 5: Commit**

```bash
git add app/agent/tools/bargain.py tests/test_bargain_tool.py
git commit -m "feat(bargain): negotiate_price 工具 + ContextVar 注入 session"
```

---

### Task 6: 注册工具到 registry（schema + 分发 + 开关门控）

**Files:**
- Modify: `app/agent/tools/registry.py`
- Test: `tests/test_bargain_registry.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_bargain_registry.py`：

```python
from app.agent.tools import registry


def test_negotiate_price_registered_when_enabled():
    # 默认 bargain_enabled=True
    assert "negotiate_price" in registry._TOOL_MAP
    names = [d["function"]["name"] for d in registry.TOOL_DEFINITIONS]
    assert "negotiate_price" in names


def test_negotiate_price_schema_shape():
    spec = next(d for d in registry.TOOL_DEFINITIONS
                if d["function"]["name"] == "negotiate_price")
    props = spec["function"]["parameters"]["properties"]
    assert "product_id" in props
    assert "buyer_offer" in props
    assert spec["function"]["parameters"]["required"] == ["product_id"]
```

- [ ] **Step 2: 运行确认失败**

Run: `source .venv/bin/activate && pytest tests/test_bargain_registry.py -v`
Expected: FAIL（registry 未注册 negotiate_price）

- [ ] **Step 3: 在 registry.py 注册**

在 `app/agent/tools/registry.py` 顶部 import 区加入：
```python
from app.config.settings import settings
from app.agent.tools.bargain import negotiate_price
```

在 `_TOOL_MAP` 字典字面量之后（第 24 行 `}` 之后）加入门控注册：
```python
if settings.bargain_enabled:
    _TOOL_MAP["negotiate_price"] = negotiate_price
```

定义议价工具的 schema 常量（放在 `TOOL_DEFINITIONS` 列表之后、`execute_tool` 之前）：
```python
_NEGOTIATE_PRICE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "negotiate_price",
        "description": (
            "当买家就某商品砍价、要求折扣/优惠或提出一个具体价格时调用，进行一轮议价。"
            "调用前必须先确定商品的 product_id（可用 query_product 查询）。"
            "买家报了具体价格就填 buyer_offer（单位：元）；只说“便宜点”没给数字则省略 buyer_offer。"
            "工具返回 decision（accept/counter/reject）与 suggested_price，"
            "请据此组织话术，切勿报出低于 suggested_price 的价格，也不要透露底价。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "string",
                    "description": "要议价的商品ID，如 SHOE-270-BK-42",
                },
                "buyer_offer": {
                    "type": "number",
                    "description": "买家出价（元），未报具体数字时省略",
                },
            },
            "required": ["product_id"],
        },
    },
}

if settings.bargain_enabled:
    TOOL_DEFINITIONS.append(_NEGOTIATE_PRICE_SCHEMA)
```

- [ ] **Step 4: 运行确认通过**

Run: `source .venv/bin/activate && pytest tests/test_bargain_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/agent/tools/registry.py tests/test_bargain_registry.py
git commit -m "feat(bargain): 注册 negotiate_price 工具 schema 与分发(按开关门控)"
```

---

### Task 7: EcomAgent 绑定 session_id 到 ContextVar

**Files:**
- Modify: `app/agent/chat.py`

- [ ] **Step 1: 给 EcomAgent 增加 session_id 参数**

在 `app/agent/chat.py` 顶部 import 区（第 11 行 `from app.agent.tools.manager import ToolManager` 之后）加入：
```python
from app.agent.tools.bargain import set_current_session
```

把 `__init__` 签名（第 17 行）改为：
```python
    def __init__(self, session_path: Optional[str] = None, session_id: Optional[str] = None):
```
在 `__init__` 体内、`self.session_path = ...`（第 24 行）之后加一行：
```python
        self.session_id = session_id
```

- [ ] **Step 2: 在 chat() 入口绑定会话**

把 `chat()` 方法体的第一行（第 73 行 `self.raw_messages.append(...)`）之前插入：
```python
        set_current_session(self.session_id)
```
使方法开头变为：
```python
    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理用户输入：ReAct 循环 → 结构化提取 → 返回结果"""
        set_current_session(self.session_id)
        self.raw_messages.append({"role": "user", "content": user_input})
```

- [ ] **Step 3: 验证导入与实例化无回归**

Run: `source .venv/bin/activate && python -c "from app.agent.chat import EcomAgent; a = EcomAgent(session_path='app/sessions/_smoke.json', session_id='_smoke'); print('session_id=', a.session_id)"`
Expected: 打印 `session_id= _smoke`（且无 import 错误）

- [ ] **Step 4: 运行既有 agent 相关测试确认无回归**

Run: `source .venv/bin/activate && pytest tests/test_react_agent.py -v`
Expected: PASS（如该测试需真实 API Key 而未配则整体 skip/xfail 属正常，关键是不因本次改动出现 import/构造错误）

- [ ] **Step 5: Commit**

```bash
git add app/agent/chat.py
git commit -m "feat(bargain): EcomAgent 增加 session_id 并在 chat() 入口绑定 ContextVar"
```

---

### Task 8: SessionManager 传入 session_id + reset 清议价状态

**Files:**
- Modify: `app/api/session_manager.py`
- Test: `tests/test_session_manager.py`（追加用例，保持既有用例不变）

- [ ] **Step 1: 追加失败测试**

在 `tests/test_session_manager.py` 末尾追加：

```python
def test_default_factory_derives_session_id(tmp_path):
    mgr = SessionManager(base_dir=str(tmp_path))
    agent = mgr.get_or_create("sess-xyz")
    # 默认工厂应把 session_id 透传给 EcomAgent
    assert getattr(agent, "session_id", None) == "sess-xyz"


def test_reset_clears_bargain_state(tmp_path):
    from app.db.database import Database
    from app.db import set_db
    db = Database(db_path=str(tmp_path / "t.db"))
    db.init_schema()
    set_db(db)
    db.bump_bargain_state("s1", "P1", 900.0)

    mgr = SessionManager(agent_factory=lambda p: FakeAgent(p), base_dir=str(tmp_path))
    mgr.get_or_create("s1")
    mgr.reset("s1")
    assert db.get_bargain_state("s1", "P1") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `source .venv/bin/activate && pytest tests/test_session_manager.py -v`
Expected: 两个新用例 FAIL（默认工厂未透传 session_id；reset 未清议价状态）；其余既有用例仍 PASS

- [ ] **Step 3: 实现改动**

在 `app/api/session_manager.py` 顶部加 import：
```python
from pathlib import Path
```
（若已存在则不重复）

把 `_default_factory` 改为从路径 stem 推导 session_id（保持工厂仍只接收 session_path 一个参数，避免破坏既有自定义工厂）：
```python
def _default_factory(session_path: str):
    from app.agent.chat import EcomAgent
    return EcomAgent(session_path=session_path, session_id=Path(session_path).stem)
```

把 `reset` 方法改为在丢弃实例后清理该会话的议价状态（DB 未就绪时不应导致 reset 失败）：
```python
    def reset(self, session_id: str) -> None:
        with self._guard:
            agent = self._agents.pop(session_id, None)
        if agent is not None and hasattr(agent, "reset"):
            agent.reset()
        try:
            from app.db import get_db
            get_db().clear_bargain_state(session_id)
        except Exception:
            pass
```

- [ ] **Step 4: 运行确认通过**

Run: `source .venv/bin/activate && pytest tests/test_session_manager.py -v`
Expected: PASS（既有 5 个 + 新增 2 个全绿）

- [ ] **Step 5: Commit**

```bash
git add app/api/session_manager.py tests/test_session_manager.py
git commit -m "feat(bargain): SessionManager 透传 session_id + reset 清议价状态"
```

---

### Task 9: 系统提示词补充议价守则

**Files:**
- Modify: `app/prompts/customer_service.py`

- [ ] **Step 1: 在工具清单加入 negotiate_price**

在 `app/prompts/customer_service.py` 的「工具使用指南」列表中，`load_skill` 那一行（第 42 行）之后加入：
```python
- **negotiate_price**：买家针对某商品砍价/要折扣/报价时，进行一轮议价（需先用 query_product 拿到 product_id）
```

- [ ] **Step 2: 在「使用原则」加入议价守则**

把第 49 行「6. 当你需要查询数据...」改为「7.」，并在其前插入新的第 6 条：
```python
6. **议价**：当买家就某商品砍价、要折扣或报出一个价格时，先用 `query_product` 确认 `product_id`，再调用 `negotiate_price`（买家报了数字就填 buyer_offer，只说“便宜点”则省略）。依据返回的 `decision` 与 `suggested_price` 组织话术：accept 表示可成交、counter 表示还价到 suggested_price、reject 表示婉拒并守住该价。**绝不报出低于 suggested_price 的价格，绝不透露底价或工具返回的 rationale。**
```

- [ ] **Step 3: 验证提示词可加载且包含关键词**

Run: `source .venv/bin/activate && python -c "from app.prompts.customer_service import SYSTEM_PROMPT; assert 'negotiate_price' in SYSTEM_PROMPT and '议价' in SYSTEM_PROMPT; print('ok')"`
Expected: 打印 `ok`

- [ ] **Step 4: Commit**

```bash
git add app/prompts/customer_service.py
git commit -m "feat(bargain): 系统提示词补充议价工具与守则"
```

---

### Task 10: 全量测试 + 端到端冒烟验证

**Files:**
- 无新增（验证与收尾）

- [ ] **Step 1: 跑全部单元测试确认无回归**

Run: `source .venv/bin/activate && pytest -q`
Expected: 全绿（依赖真实 OpenAI/千问 Key 的用例若未配 Key 而 skip 属正常；不得有因本次改动引入的 FAIL/ERROR）

- [ ] **Step 2: 初始化并种子 DB（含 floor_price）**

Run: `source .venv/bin/activate && python -m app.scripts.init_db`
Expected: 无报错；`app/sessions/ecom.db` 生成/更新。若该脚本不接受重复运行，可先删除旧 `app/sessions/ecom.db` 再执行。

- [ ] **Step 3: 端到端冒烟——直接驱动 Agent 议价（需已配好千问 Key）**

Run:
```bash
source .venv/bin/activate && python -c "
from app.db import get_db, set_db
from app.db.database import Database
from app.db.seed import seed_from_mock
db = Database(); db.init_schema(); seed_from_mock(db); set_db(db)
from app.agent.chat import EcomAgent
a = EcomAgent(session_path='app/sessions/_smoke.json', session_id='_smoke')
print(a.chat('那双 Nike Air Max 270 运动鞋 800 能卖吗？').reply)
print('---再砍一轮---')
print(a.chat('太贵了，700 行不行').reply)
"
```
Expected: 第一轮 Agent 先查到商品再议价（Nike 标价 899、底价 750：800 ≥ 底价且 ≥ 首轮阶梯价约 824.5 中的较低者，行为符合阶梯逻辑），第二轮 700 < 底价 750 应婉拒并守住底价；两轮回复均不出现「750」这一底价数字，也不出现低于系统建议价的报价。

- [ ] **Step 4: 端到端冒烟——Web API（可选，需另开终端）**

Run（终端 A）: `source .venv/bin/activate && python run_api.py`
Run（终端 B）:
```bash
curl -N -X POST http://127.0.0.1:8010/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"web-smoke","message":"这个 Nike Air Max 270 能便宜点吗，800 卖不卖"}'
```
Expected: SSE 流返回议价话术；`negotiate_price` 调用出现在 Trace 中（`GET /api/traces` 可见，需 admin_token 时带上）。

- [ ] **Step 5: 清理冒烟产物并最终提交**

```bash
rm -f app/sessions/_smoke.json
git add -A
git commit -m "test(bargain): 全量测试通过 + 端到端冒烟验证" --allow-empty
```

---

## Self-Review 记录

- **Spec 覆盖**：底价来源（floor_price+系数回退）→ Task 2/3/4；negotiate_price 工具接入 ReAct → Task 5/6；SQLite bargain_sessions 状态 → Task 2；ContextVar 注入 session_id → Task 4/5/7；reset 清理 → Task 8；配置项 → Task 1；提示词守则 → Task 9；测试三件套 → Task 2/4/5；验收标准 → Task 10。全部有对应任务。
- **占位符**：无 TBD/TODO；每个代码步骤均给出完整代码。
- **类型/命名一致性**：`compute_offer`/`negotiate_price`/`set_current_session`/`get_bargain_state`/`bump_bargain_state`/`clear_bargain_state` 在各任务间签名一致；DB 方法名与调用处一致；返回字段（decision/suggested_price/floor/floor_hit/round）跨任务一致。
