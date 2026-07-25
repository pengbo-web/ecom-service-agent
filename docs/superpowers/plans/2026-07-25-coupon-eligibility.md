# 优惠券按用户资格过滤 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** `query_coupons` 从"返回写死的全量券目录"改为"按**当前用户资格**(会员等级 + 是否新客)筛选出可领券,并附不可领券及原因"——修掉"给非会员推会员券、给老客推新人券"的体验缺口,同时让模型有真实资格数据可依、不再自行编造"会员专享/可叠加"。

**Architecture:** 补齐两个资格数据源(`users.member_level` 迁移+种子;`Database.count_user_orders` 精确按 user 统计),用一个 `current_user` ContextVar 把当前登录身份传到工具层(与 `set_current_session`/`set_memory_manager` 同处每轮刷新),`query_coupons` 据此过滤。全程 **fail-open**:拿不到用户/资格时退回展示全部券(不漏发)。券的受众规则(new/member/all)写进券数据,判定是纯函数便于测试。

**Tech Stack:** 现有 SQLite/tools/EcomAgent、标准库 contextvars。零新依赖。

## Global Constraints

- **fail-open 铁律**:`query_coupons` 拿不到 user_id、或查资格异常 → 返回**全部**券(现状行为),绝不因资格查询失败而不发券或报错。
- **受众规则(逐字)**:每张券带 `audience` ∈ `{"new","member","all"}`;判定——`new`→仅新客(该用户订单数==0);`member`→仅会员(`member_level != "normal"`);`all`→人人可领(品类券,领取无门槛,使用限制在结算时,不在本工具管)。
- **member_level 值域**:`normal`(默认)/ `silver` / `gold` / `diamond`;"是否会员" = `level != "normal"`。
- **users 迁移**:`ALTER TABLE users ADD COLUMN member_level TEXT DEFAULT 'normal'`——先 `PRAGMA table_info(users)` 检查再加(幂等,旧库安全,无破坏);`create_user` 新用户默认 `normal`(不改签名,靠列默认值)。
- **current_user 传递**:新增 `app/agent/runtime_context.py` 的 `set_current_user(uid)`/`get_current_user() -> str | None`(ContextVar,默认 None);`EcomAgent.chat()` 每轮 `set_current_user(self.user_id)`(与 `set_current_session` 同处);工具从 `get_current_user()` 取,取不到 → fail-open。
- **不改 `list_orders` 全量行为**(见文末"相关发现"——那是独立 bug,本次只**新增** `count_user_orders(user_id)` 精确统计给券用,不动既有全量查询,避免改变现有演示行为)。
- **返回结构(逐字)**:`{"success": True, "coupons": [可领券...], "unavailable": [{"code","name","reason"}...], "note": "..."}`;可领券保留原字段;fail-open 时 `unavailable=[]`、note 标注"未识别用户,展示全部券"。
- **工具定义更新**:`query_coupons` 的 description 说明"按当前用户会员等级与新老客身份返回可领券";参数仍无(user 从上下文取,不由模型传——防模型伪造身份)。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 origin,绝不 upstream;`.venv/Scripts/python.exe`;测试不 print emoji。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/db/database.py`(改) | C1 | users.member_level 迁移;`get_user` 带 member_level;`count_user_orders(user_id)` |
| `app/db/seed.py`(改) | C1 | 种子用户设会员等级 |
| `app/agent/runtime_context.py`(新) | C2 | `current_user` ContextVar |
| `app/agent/chat.py`(改一行) | C2 | 每轮 `set_current_user` |
| `app/agent/tools/order_ops.py`(改) | C2 | `query_coupons` 资格过滤 + 券 audience |
| `app/agent/tools/registry.py`(改) | C2 | 工具 description 更新 |
| `tests/test_coupon_eligibility.py`(新) | C1/C2 | 全部行为测试 |

---

### Task C1: 资格数据源(member_level 迁移 + count_user_orders)

**Files:**
- Modify: `app/db/database.py`(init_schema 迁移;`get_user` 扩返回;类尾加 `count_user_orders`)、`app/db/seed.py`
- Test: `tests/test_coupon_eligibility.py`(新)

**Interfaces:**
- Produces(C2 依赖,签名逐字):
  - `get_user(user_id) -> Optional[dict]` 返回增加 `"member_level"` 键(缺列/旧行 → `"normal"`)
  - `count_user_orders(self, user_id: str) -> int`(`SELECT COUNT(*) FROM orders WHERE user = ?`)
  - `set_member_level(self, user_id: str, level: str) -> None`(seed/运维用 upsert 更新等级)

- [ ] **Step 1: 写失败测试** `tests/test_coupon_eligibility.py`

```python
"""优惠券资格:数据源(member_level/订单数)+ 过滤。临时库,全离线。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    return db


def test_member_level_defaults_normal_and_updatable(tmp_path):
    db = _db(tmp_path)
    db.create_user("u1", "u1")
    assert db.get_user("u1")["member_level"] == "normal"      # 默认
    db.set_member_level("u1", "diamond")
    assert db.get_user("u1")["member_level"] == "diamond"


def test_get_user_missing_still_none(tmp_path):
    assert _db(tmp_path).get_user("ghost") is None


def test_count_user_orders(tmp_path):
    db = _db(tmp_path)
    conn = __import__("sqlite3").connect(db.db_path)
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O1','u1','pending',10,'t')")
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O2','u1','pending',20,'t')")
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O3','u2','pending',30,'t')")
    conn.commit(); conn.close()
    assert db.count_user_orders("u1") == 2
    assert db.count_user_orders("newbie") == 0           # 新客


def test_migration_idempotent_on_old_users_table(tmp_path):
    """旧库(users 无 member_level)init_schema 幂等加列,不炸、不丢数据。"""
    import sqlite3
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users VALUES ('老用户','老用户')")
    conn.commit(); conn.close()
    db = Database(p); db.init_schema(); db.init_schema()   # 两次,验证幂等
    assert db.get_user("老用户")["member_level"] == "normal"   # 旧行补默认值
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_coupon_eligibility.py -q` FAIL

- [ ] **Step 3: 实现 database.py**——init_schema 的 executescript **之后**加迁移(参考已有短连接;executescript 里 users 建表语句也补 `member_level TEXT DEFAULT 'normal'` 兼顾新库):

```python
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "member_level" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN member_level TEXT DEFAULT 'normal'")
                conn.commit()
```

`get_user` 改 SELECT 带 member_level(缺值兜 normal):

```python
    def get_user(self, user_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT user_id, name, member_level FROM users WHERE user_id = ?",
                (user_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["member_level"] = d.get("member_level") or "normal"
            return d
        finally:
            conn.close()
```

类尾加两方法:

```python
    def count_user_orders(self, user_id: str) -> int:
        conn = self.connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM orders WHERE user = ?",
                               (user_id,)).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def set_member_level(self, user_id: str, level: str) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE users SET member_level = ? WHERE user_id = ?",
                         (level, user_id))
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 4: seed.py**——种子用户建好后设等级(在 `for name in users:` 之后加):

```python
        # 种子会员等级(演示资格过滤:部分用户是会员)
        _LEVELS = {"小明": "diamond", "小红": "gold"}
        for name, lvl in _LEVELS.items():
            if name in users:
                conn.execute("UPDATE users SET member_level = ? WHERE user_id = ?", (lvl, name))
```

- [ ] **Step 5: 跑通过** → `pytest tests/test_coupon_eligibility.py tests/test_db_schema.py tests/test_users_db.py tests/test_users_api.py -q` PASS(users_api 验证 create_user 后 member_level=normal 不破坏 auth)

- [ ] **Step 6: 提交** `feat(db): C1 users.member_level 迁移 + count_user_orders(券资格数据源)`

---

### Task C2: current_user 上下文 + query_coupons 资格过滤

**Files:**
- Create: `app/agent/runtime_context.py`
- Modify: `app/agent/chat.py`(chat 开头)、`app/agent/tools/order_ops.py`、`app/agent/tools/registry.py`
- Test: `tests/test_coupon_eligibility.py`(增)

**Interfaces:**
- Consumes: C1 的 `get_user`(member_level)、`count_user_orders`;`get_db()`。
- Produces: `set_current_user(uid: str | None)`/`get_current_user() -> str | None`;`query_coupons()` 按资格过滤(返回结构见全局约束);纯函数 `filter_coupons(coupons, is_new, is_member) -> tuple[list, list]`(返回 (可领, 不可领带 reason))。

- [ ] **Step 1: 写失败测试**(增到 `tests/test_coupon_eligibility.py`)

```python
from app.agent.runtime_context import set_current_user, get_current_user
from app.agent.tools.order_ops import query_coupons, filter_coupons, _COUPONS


def test_filter_coupons_pure():
    # 新客+会员:新人券✓ 会员券✓ 全场券✓
    ok, no = filter_coupons(_COUPONS, is_new=True, is_member=True)
    codes = {c["code"] for c in ok}
    assert {"NEW20", "VIP90", "SHOE30"} <= codes and no == []
    # 老客+非会员:只剩全场券,另两张带原因
    ok, no = filter_coupons(_COUPONS, is_new=False, is_member=False)
    assert {c["code"] for c in ok} == {"SHOE30"}
    reasons = {c["code"]: c["reason"] for c in no}
    assert "NEW20" in reasons and "VIP90" in reasons


def test_query_coupons_filters_by_user(tmp_path, monkeypatch):
    from app.db import Database, set_db
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    db.create_user("newbie", "newbie")            # 新客,normal
    db.create_user("vip", "vip"); db.set_member_level("vip", "gold")
    import sqlite3
    conn = sqlite3.connect(db.db_path)            # vip 有 1 单 → 老客
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O1','vip','pending',10,'t')"); conn.commit(); conn.close()
    set_db(db)
    try:
        set_current_user("newbie")
        r = query_coupons()
        assert "NEW20" in {c["code"] for c in r["coupons"]}     # 新客有新人券
        assert "VIP90" not in {c["code"] for c in r["coupons"]} # 非会员无会员券
        set_current_user("vip")
        r = query_coupons()
        assert "VIP90" in {c["code"] for c in r["coupons"]}     # 会员有会员券
        assert "NEW20" not in {c["code"] for c in r["coupons"]} # 老客无新人券
        assert any(u["code"] == "NEW20" for u in r["unavailable"])
    finally:
        set_db(None); set_current_user(None)


def test_query_coupons_fail_open_without_user(tmp_path, monkeypatch):
    from app.db import Database, set_db
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    try:
        set_current_user(None)                    # 无当前用户
        r = query_coupons()
        assert len(r["coupons"]) == len(_COUPONS)  # fail-open:全部券
        assert r["unavailable"] == []
    finally:
        set_db(None)


def test_context_var_roundtrip():
    set_current_user("u9")
    assert get_current_user() == "u9"
    set_current_user(None)
    assert get_current_user() is None
```

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现** `app/agent/runtime_context.py`

```python
"""运行时上下文:把当前登录用户传到工具层(与 bargain.current_session 同模式)。

工具(如 query_coupons)据此按用户资格个性化,而不信任模型传参——防伪造身份。
EcomAgent.chat() 每轮刷新;取不到时工具应 fail-open。
"""

from __future__ import annotations

import contextvars
from typing import Optional

_current_user: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_user", default=None)


def set_current_user(user_id: Optional[str]) -> None:
    _current_user.set(user_id)


def get_current_user() -> Optional[str]:
    return _current_user.get()
```

`order_ops.py`——券加 audience,加纯过滤函数,重写 query_coupons:

```python
_COUPONS = [
    {"code": "NEW20", "name": "新人券", "discount": "满100减20", "expires": "2026-12-31", "audience": "new"},
    {"code": "VIP90", "name": "会员9折券", "discount": "9折(最高减50)", "expires": "2026-09-30", "audience": "member"},
    {"code": "SHOE30", "name": "鞋类专享券", "discount": "满300减30", "expires": "2026-08-31", "audience": "all"},
]

_AUDIENCE_REASON = {
    "new": "仅限新客(您已有历史订单)",
    "member": "仅限会员(开通会员后可领)",
}


def filter_coupons(coupons: list, is_new: bool, is_member: bool):
    """按资格切分:返回 (可领列表, 不可领列表[带 reason])。纯函数,便于测试。"""
    ok, no = [], []
    for c in coupons:
        aud = c.get("audience", "all")
        eligible = (aud == "all") or (aud == "new" and is_new) or (aud == "member" and is_member)
        if eligible:
            ok.append({k: v for k, v in c.items() if k != "audience"})
        else:
            no.append({"code": c["code"], "name": c["name"],
                       "reason": _AUDIENCE_REASON.get(aud, "当前不可领")})
    return ok, no


def query_coupons() -> dict:
    """查询当前用户可领的优惠券(按会员等级 + 新老客身份筛选,只读)。

    fail-open:识别不到当前用户或查资格异常 → 返回全部券,不漏发。
    """
    from app.agent.runtime_context import get_current_user
    uid = get_current_user()
    if not uid:
        return {"success": True, "coupons": [{k: v for k, v in c.items() if k != "audience"}
                                             for c in _COUPONS],
                "unavailable": [], "note": "未识别当前用户,已展示全部券。"}
    try:
        from app.db import get_db
        db = get_db()
        user = db.get_user(uid)
        is_member = bool(user) and (user.get("member_level") or "normal") != "normal"
        is_new = db.count_user_orders(uid) == 0
        ok, no = filter_coupons(_COUPONS, is_new=is_new, is_member=is_member)
        return {"success": True, "coupons": ok, "unavailable": no,
                "note": "以上为您当前可领的优惠券(已按会员等级与新老客身份筛选)。"}
    except Exception:
        return {"success": True, "coupons": [{k: v for k, v in c.items() if k != "audience"}
                                             for c in _COUPONS],
                "unavailable": [], "note": "未识别当前用户,已展示全部券。"}
```

`chat.py`——`chat()` 开头 `set_current_session(self.session_id)` 附近加:

```python
        from app.agent.runtime_context import set_current_user
        set_current_user(self.user_id)
```

`registry.py`——`query_coupons` 的 description 改为:

```python
            "description": (
                "查询当前用户【可领】的优惠券,已按其会员等级与新老客身份筛选。"
                "无需参数(用户身份由服务端上下文确定)。返回 coupons(可领)与 "
                "unavailable(不可领及原因);请只向用户介绍 coupons 里的券,"
                "unavailable 仅供你判断,不要承诺用户能用不可领的券。"
            ),
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_coupon_eligibility.py tests/test_order_ops.py tests/test_react_degrade.py tests/test_controller_agent.py -q` PASS(order_ops 现有测试若断言 query_coupons 旧结构需同步为新结构——本任务范围内适配)

- [ ] **Step 5: 提交** `feat(tools): C2 query_coupons 按用户资格过滤 + current_user 上下文`

---

### Task C3: 端到端冒烟(控制方执行)

- [ ] 重灌种子(`.venv/Scripts/python.exe -m app.scripts.init_db` 或等价)使 users.member_level 生效;确认 `小明`=diamond
- [ ] 登录新用户 `pengbo`(无订单、normal)→ 问"有哪些优惠券" → 只见**新人券**(新客),**无会员券**;活动面板 `query_coupons` 结果 coupons 不含 VIP90
- [ ] 登录 `小明`(diamond、有订单)→ 问优惠券 → 见**会员券**,**无新人券**(老客)
- [ ] Langfuse 该轮 trace:`execute_tool query_coupons` 的 output 已是筛选后结果(不再全量);且回复不再编造"可叠加/会员专享"(有真实资格数据)
- [ ] 关联验证:非会员用户不再被推会员券(修复目标达成)
- [ ] `.superpowers/sdd/progress.md` 记账

## 相关发现(不在本次范围,建议另修)

`Database.list_orders()` 是 `SELECT * FROM orders`(全量),`list_user_orders()` 工具据此返回**所有用户的订单**——即任何登录用户问"我有哪些订单"都会看到全部 5 单(跨用户越权 + 不准确)。本次为券资格新增了 `count_user_orders(user_id)` 精确统计(不动 list_orders 避免改变现有演示),但 `list_user_orders` 的越权是独立且更严重的问题,建议**另开工单**:让它用 `current_user` + 按 user 过滤(与本次 current_user 基建可复用)。已在 ledger 记录。

## 总量与顺序

C1(~0.4d)→ C2(~0.5d)→ C3(~0.1d),共 **~1 人日**。

## Self-Review

- **覆盖核对**:member_level 迁移+种子(C1)✅;count_user_orders(C1)✅;current_user ContextVar+chat 接线(C2)✅;query_coupons 按(会员+新客)过滤+unavailable 原因(C2,filter_coupons 纯函数测试+集成测试)✅;fail-open 无 user/异常(C2 两条测试)✅;工具 description 更新(C2)✅;list_orders 越权作为相关发现单列(不纳入)✅。
- **占位符扫描**:seed/init_db 重灌命令在 C3 给了具体入口(`app.scripts.init_db`,与 H3.2 提到的同一脚本);order_ops 现有测试"需同步为新结构"标注了本任务范围内适配;无 TBD。
- **类型一致性**:`filter_coupons(coupons, is_new, is_member) -> (ok, no)`、`query_coupons() -> dict{coupons,unavailable,note}`、`get_user()["member_level"]`、`count_user_orders(user_id) -> int`、`set_current_user/get_current_user` 在 C1 定义、C2 消费/测试全一致;`audience` 字段值域 new/member/all 与 `_AUDIENCE_REASON` 键一致。
- **已知取舍**:①券受众只做 new/member/all 三档(YAGNI,不做金额/品类动态门槛——SHOE30 的鞋类限制留在结算,本工具不判);②member_level 无"降级/过期"逻辑(演示;真实会员体系另说);③fail-open 宁可多展示不漏发(客服场景对"少发券"比"多发券"更敏感)。
