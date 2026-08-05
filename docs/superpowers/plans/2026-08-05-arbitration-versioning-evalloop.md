# 冲突仲裁 / Skill 版本身份 / 评测闭环 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补三个经代码核实的真缺口——① 触达审批缺状态仲裁（能真伤到用户）；② skill 轨迹缺版本身份（两次转正后 A/B 归因断链）；③ 人工回复进了语料蒸馏但没进评测期望，回流用例也不进回归集。

**Architecture:** 三块互相独立，可任意顺序。都不新增子系统，都是把已有的东西接上：仲裁复用 `HitlManager.manual_mode` 与 `HandoffQueue`；版本身份复用 `promote_skill` 的备份时间戳与 `skill_traces`；评测闭环复用 `trace_to_case` 与 `golden_corpus` 已经在读的同一批人工回复。

**Tech Stack:** Python 3.11 / FastAPI / SQLite / pytest

---

## 核实过的现状（写方案前实读代码）

| 事实 | 位置 | 结论 |
|---|---|---|
| `approve_draft` 投递前**零状态检查** | `app/api/app.py` 审批端点 | ① 是真缺口 |
| `ManualMode` 是**内存态 + 超时回落**，不持久化 | `app/hitl/manual_mode.py` | 仲裁只能查内存，进程重启后接管态丢失——要在方案里讲明这条边界 |
| `HandoffQueue` 持久在**独立 SQLite**（`settings.hitl_db_path`），非主库 | `app/hitl/queue.py` | 未结工单可持久查询,是比内存接管态更可靠的仲裁依据 |
| `skill_traces` 列：`id/session_id/user_id/skill_name/tool_calls/outcome/created_at/variant` | `app/db/database.py` | ② 无版本列,证实 |
| 备份目录名形如 `<skill>/20260804-232032/` | `definitions/_archive/` 实测 | 有时间戳但与轨迹无链接 |
| `golden_corpus.synthesize_from_golden` **已接入** | `app/scripts/synthesize_skills.py:260` | ③ 的"语料侧"已闭,不用重做 |
| `trace_to_case` 从不设 `expected_keywords` | `app/evaluation/trace_to_case.py:24-45` | ③ 的真缺口之一 |
| 回流用例落 `app/sessions/reflow_cases.json`,与 `app/evaluation/cases.json` 无合并路径 | `app/scripts/reflow_traces.py` | ③ 的真缺口之二 |

---

## Global Constraints

1. **不可逆动作的既有闸门只能加严不能放松**：仲裁是在人工批准**之后**再加一道，不替代人工审批。
2. **仲裁失败必须 fail-closed**：查接管态/工单时抛异常 → 视为"不可发"，而不是放行。理由：这条闸守的是"别给正在投诉的人推销"，查不清就不发的代价远小于发错。
3. **幂等不能被破坏**：现有 `review_outreach_draft` 的条件更新是防重发的关键。仲裁必须在**认领之前**判定，否则会出现"认领成功→仲裁拒绝→退回"的多余状态翻转。
4. **版本号只增不减，且与轨迹同源**：轨迹里记的版本必须是**加载时**那一刻 live 目录的版本，不是查询时的版本。
5. **旧库兼容**：新列走 `PRAGMA table_info` 补列，历史行给确定的默认值。
6. **评测期望不得凭猜**：`expected_keywords` 只能来自真实人工回复的抽取，抽不出就留空（留空在评分时被跳过，不计 0）。
7. **回流合并不得覆盖人工维护的用例**：合并按 id 去重，已存在的 id 不覆盖。
8. 中文注释与 docstring；只在 `feature/w1-service-streaming` 提交，不建分支不推送。

---

## Task 1: 触达审批的状态仲裁

**Files:**
- Create: `app/multi_agent/arbitration.py`
- Modify: `app/api/app.py`（审批端点）
- Test: `tests/test_arbitration.py`

**Interfaces:**
- Produces:
  - `BLOCK_MANUAL = "manual_takeover"` / `BLOCK_OPEN_HANDOFF = "open_handoff"` / `BLOCK_UNKNOWN = "arbitration_failed"`
  - `check_outreach_allowed(user_id: str, hitl=None, db=None) -> tuple[bool, str]`
    返回 `(allowed, reason)`；`reason` 是给店主看的中文说明，allowed 时为 `""`

**为什么按 user_id 而不是 session_id**：草稿记的是 `user_id`（营销面向"人"，不面向某一次会话）；而接管态与工单挂在 `session_id` 上。所以仲裁要先由 `user_id` 找到该买家**当前会话**（`Database.latest_conversation`），再查那个会话的状态。这一跳必须写在注释里——它是这条闸唯一容易错的地方。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_arbitration.py`：

```python
"""触达仲裁:正在投诉/人工接管的买家不得被推销;查不清则不发。"""

import pytest

from app.multi_agent import arbitration as arb
from app.db.database import Database


class FakeManual:
    def __init__(self, manual_sessions=()):
        self._m = set(manual_sessions)
    def is_manual(self, session_id):
        return session_id in self._m


class FakeHitl:
    def __init__(self, manual_sessions=()):
        self.manual_mode = FakeManual(manual_sessions)


class FakeQueue:
    def __init__(self, pending=()):
        self._p = list(pending)
    def list_pending(self):
        return [{"session_id": s} for s in self._p]


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    conv = d.create_conversation("u1")
    return d, conv["conversation_id"]


def test_allows_ordinary_buyer(db):
    d, sid = db
    ok, reason = arb.check_outreach_allowed("u1", hitl=FakeHitl(), db=d)
    assert ok is True and reason == ""


def test_blocks_buyer_in_manual_takeover(db):
    """顾客正等人工处理,同时收到营销话术——这条闸就是为了挡住它。"""
    d, sid = db
    ok, reason = arb.check_outreach_allowed("u1", hitl=FakeHitl([sid]), db=d)
    assert ok is False
    assert "人工" in reason


def test_blocks_buyer_with_open_handoff(db):
    d, sid = db
    h = FakeHitl()
    h.queue = FakeQueue([sid])
    ok, reason = arb.check_outreach_allowed("u1", hitl=h, db=d)
    assert ok is False
    assert "工单" in reason


def test_resolved_handoff_does_not_block(db):
    d, sid = db
    h = FakeHitl()
    h.queue = FakeQueue([])          # 已结工单不在 pending 列表里
    ok, _ = arb.check_outreach_allowed("u1", hitl=h, db=d)
    assert ok is True


def test_buyer_with_no_conversation_is_allowed(db):
    """没有会话的买家谈不上"正在投诉",不该被这条闸拦住(否则新客永远收不到)。"""
    d, _ = db
    ok, reason = arb.check_outreach_allowed("nobody", hitl=FakeHitl(), db=d)
    assert ok is True and reason == ""


def test_fails_closed_when_state_unreadable(db):
    """查不清状态时必须判不可发——发错的代价远大于漏发。"""
    d, sid = db

    class Boom:
        @property
        def manual_mode(self):
            raise RuntimeError("hitl down")

    ok, reason = arb.check_outreach_allowed("u1", hitl=Boom(), db=d)
    assert ok is False
    assert "无法确认" in reason


def test_no_hitl_configured_is_allowed(db):
    """HITL 整个关掉时不该把营销也锁死(该部署里没有"正在投诉"这个状态)。"""
    d, _ = db
    ok, reason = arb.check_outreach_allowed("u1", hitl=None, db=d)
    assert ok is True and reason == ""
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_arbitration.py -q
```
Expected: FAIL，`ModuleNotFoundError: app.multi_agent.arbitration`

- [ ] **Step 3: 实现 `app/multi_agent/arbitration.py`**

```python
"""跨 Agent 冲突仲裁:阻止一个 Agent 的动作撞上另一条链正在处理的事。

当前唯一一条真实冲突:营销 Agent 的触达草稿是"退款率异常"这条协作链产的,
与某个具体买家**当下的状态**无关。于是一个正在投诉、会话已转人工接管的顾客,
照样可能收到"这款鞋不少朋友反馈偏大,需要帮您确认吗"。

collab.py 里已经挡了"escalation 信号不触发营销",但那只管**信号侧**:草稿不是
由这个买家的升级产生的,所以那道闸对它不生效。仲裁必须放在**投递侧**。

fail-closed:查不清状态一律判不可发。这条闸守的是"别给正在投诉的人推销",
查不清就不发的代价远小于发错。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

BLOCK_MANUAL = "manual_takeover"
BLOCK_OPEN_HANDOFF = "open_handoff"
BLOCK_UNKNOWN = "arbitration_failed"


def check_outreach_allowed(user_id: str, hitl=None,
                           db=None) -> tuple[bool, str]:
    """该买家当下是否可以接收营销触达。返回 (allowed, 中文原因)。

    **user_id → session_id 这一跳是本函数唯一容易错的地方**:草稿记的是
    user_id(营销面向"人"),而接管态与工单挂在 session_id 上,所以必须先由
    user_id 找到该买家当前会话,再查那个会话的状态。

    没有会话的买家(新客)判可发——他谈不上"正在投诉",拿这条闸拦他会让
    营销永远碰不到新客。
    """
    uid = (user_id or "").strip()
    if not uid:
        return False, "草稿没有目标买家,无法确认其当前状态,已拒绝发送。"
    if hitl is None:
        return True, ""      # 该部署没开 HITL,不存在"正在投诉"这个状态

    if db is None:
        from app.db import get_db
        db = get_db()

    try:
        conv = db.latest_conversation(uid)
        if not conv:
            return True, ""
        sid = conv.get("conversation_id") or ""
        if not sid:
            return True, ""

        if hitl.manual_mode.is_manual(sid):
            return False, ("该买家的会话正由人工客服接管中,"
                           "此时发营销消息会打断人工处理,已拒绝发送。")

        queue = getattr(hitl, "queue", None)
        if queue is not None:
            pending = queue.list_pending() or []
            if any((p.get("session_id") or "") == sid for p in pending):
                return False, ("该买家有未结的人工工单(正在等处理),"
                               "此时发营销消息不合适,已拒绝发送。")
    except Exception as exc:  # noqa: BLE001 fail-closed:查不清一律不发
        logger.exception("触达仲裁查询失败,按不可发处理 user=%s: %s", uid, exc)
        return False, "无法确认该买家当前是否在人工处理中,出于谨慎已拒绝发送。"

    return True, ""
```

- [ ] **Step 4: 接进审批端点**

在 `app/api/app.py` 的 `approve_draft` 里，`get_outreach_draft` 拿到草稿**之后**、`review_outreach_draft` 认领**之前**插入：

```python
        # 冲突仲裁:人已经点了批准,但该买家此刻可能正等人工处理——
        # 必须在**认领之前**判,否则会出现"认领成功→仲裁拒绝→退回"的多余翻转,
        # 白白消耗掉这条草稿的一次幂等机会。
        from app.multi_agent.arbitration import check_outreach_allowed
        allowed, arb_reason = check_outreach_allowed(
            draft.get("user_id", ""), hitl=hitl)
        if not allowed:
            return {"success": False, "sent": False, "reason": arb_reason}
```

- [ ] **Step 5: 补端点级测试**

在 `tests/test_growth_api.py` 追加：

```python
def test_approve_blocked_when_buyer_in_manual_takeover(client, draft, monkeypatch):
    """端点级:仲裁拒绝时不投递、不改草稿状态,草稿仍留在待审列表可重试。"""
    from app.api import app as appmod
    from app.db import get_db
    sent = []
    monkeypatch.setattr(appmod, "_deliver_outreach", lambda d: sent.append(1) or True)
    monkeypatch.setattr("app.multi_agent.arbitration.check_outreach_allowed",
                        lambda user_id, hitl=None, db=None: (False, "该买家的会话正由人工客服接管中"))
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert "人工" in body["reason"]
    assert sent == []                                        # 没投递
    assert get_db().get_outreach_draft(draft)["status"] == "draft"   # 状态没被消耗
```

- [ ] **Step 6: 跑测试 + 提交**

```bash
.venv/Scripts/python.exe -m pytest tests/test_arbitration.py tests/test_growth_api.py tests/test_hitl.py -q
```
（`tests/test_hitl.py` 若不存在，改跑实际的 HITL 测试文件，自行发现文件名。）

```bash
git add app/multi_agent/arbitration.py app/api/app.py tests/test_arbitration.py tests/test_growth_api.py
git commit -m "feat(collab): 触达投递前的冲突仲裁(人工接管/未结工单一律不发,fail-closed)"
```

**已知边界，必须写进模块 docstring**：`ManualMode` 是内存态且带超时回落，所以进程重启后"正在接管"这个信息会丢，此时仲裁只能靠未结工单兜。工单持久在独立 SQLite，是更可靠的那一半。

---

## Task 2: Skill 版本身份

**Files:**
- Modify: `app/db/database.py`（`skill_traces` 补列 + 写入方法签名）、`app/agent/skills/loader.py`（加载时带出版本）、`app/agent/skills/execution_trace.py`（轨迹带版本）、`app/agent/chat.py`（落库传版本）、`app/scripts/promote_skill.py`（转正时递增版本）
- Create: `app/agent/skills/versioning.py`
- Test: `tests/test_skill_versioning.py`

**Interfaces:**
- Produces（`app/agent/skills/versioning.py`）：
  - `VERSION_FILE = ".version"`
  - `read_version(skill_dir: Path) -> int`（无文件 → `1`，坏内容 → `1`）
  - `bump_version(skill_dir: Path) -> int`（写入并返回新版本号）
  - `Database.record_skill_trace(..., skill_version: int = 0)`（`0` = 未知，历史行同值）
  - `SkillManager.load_skill()` 返回值新增 `"version": int`
  - `SkillTurn.note_preloaded(...)` / `note_tool_call(...)` 之外新增 `set_version(v: int)`；`SkillTurn.skill_version` 属性

**为什么用目录里的 `.version` 文件而不是数据库表**：技能的身份跟着**目录**走——上传、转正、回滚都是整目录搬运（`_replace_tree`），版本号放在目录里就随搬运天然一致；放数据库需要额外维护"目录状态与表状态同步"，而这个特性已经被 TOCTOU 咬过一次。

**关键约束（Global Constraint 4）**：轨迹里记的版本必须是**加载那一刻**的版本。`load_skill` 读到的版本要一路带到轨迹落库，不能在落库时重新去磁盘读——中间可能已经发生转正。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_skill_versioning.py`：

```python
"""Skill 版本身份:目录自带版本、转正递增、轨迹记加载那一刻的版本。"""

import pytest

from app.agent.skills import versioning as V
from app.db.database import Database


def test_missing_version_file_reads_as_one(tmp_path):
    assert V.read_version(tmp_path) == 1


def test_bump_creates_and_increments(tmp_path):
    assert V.bump_version(tmp_path) == 2
    assert V.read_version(tmp_path) == 2
    assert V.bump_version(tmp_path) == 3


def test_corrupt_version_file_reads_as_one(tmp_path):
    (tmp_path / V.VERSION_FILE).write_text("不是数字", encoding="utf-8")
    assert V.read_version(tmp_path) == 1


def test_version_travels_with_the_directory(tmp_path):
    """版本号放目录里,所以上传/转正/回滚的整目录搬运天然带着它走。"""
    import shutil
    src = tmp_path / "a"; src.mkdir()
    V.bump_version(src); V.bump_version(src)          # -> 3
    dst = tmp_path / "b"
    shutil.copytree(src, dst)
    assert V.read_version(dst) == 3


def test_trace_records_version(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    d.record_skill_trace("s1", "u1", "track-order", [], "success", skill_version=7)
    t = d.list_skill_traces(limit=1)[0]
    assert t["skill_version"] == 7


def test_trace_version_defaults_to_unknown(tmp_path):
    """老调用方不传版本时落 0=未知,而不是假装是第 1 版。"""
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    d.record_skill_trace("s1", "u1", "track-order", [], "success")
    assert d.list_skill_traces(limit=1)[0]["skill_version"] == 0


def test_load_skill_exposes_version(tmp_path):
    from app.agent.skills.loader import SkillManager
    sd = tmp_path / "demo"; sd.mkdir()
    (sd / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 测试用。适用关键词:测试。\n---\n\n# x\n1. 用 `search_knowledge` 查。\n",
        encoding="utf-8")
    V.bump_version(sd)          # -> 2
    m = SkillManager(skills_dir=str(tmp_path))
    r = m.load_skill("demo")
    assert r["success"] is True
    assert r["version"] == 2


def test_ab_attribution_survives_two_promotions(tmp_path):
    """本任务存在的理由:同一 skill 转正两次后,仍能按版本把轨迹分开归因。"""
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    for _ in range(3):
        d.record_skill_trace("s", "u", "track-order", [], "tool_error", skill_version=1)
    for _ in range(2):
        d.record_skill_trace("s", "u", "track-order", [], "success", skill_version=2)
    traces = d.list_skill_traces(skill_name="track-order", limit=99)
    v1 = [t for t in traces if t["skill_version"] == 1]
    v2 = [t for t in traces if t["skill_version"] == 2]
    assert len(v1) == 3 and all(t["outcome"] == "tool_error" for t in v1)
    assert len(v2) == 2 and all(t["outcome"] == "success" for t in v2)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_skill_versioning.py -q
```
Expected: FAIL，`ModuleNotFoundError: app.agent.skills.versioning`

- [ ] **Step 3: 实现 `app/agent/skills/versioning.py`**

```python
"""Skill 版本身份:版本号写在技能目录里,随目录搬运天然一致。

为什么不放数据库:技能的身份跟着**目录**走——上传、转正、回滚都是整目录搬运
(见 promote_skill._replace_tree),版本号放目录里就自动跟着走;放数据库要额外
维护"目录状态与表状态同步",而这条链已经被 TOCTOU 咬过一次(见转正快照的注释)。

读不到/读坏一律回落 1:版本号是用来**归因**的,坏数据不该让加载失败。
"""

from __future__ import annotations

from pathlib import Path

VERSION_FILE = ".version"


def read_version(skill_dir: Path) -> int:
    """读该技能目录的版本号。无文件/坏内容/非正整数 → 1。"""
    try:
        raw = (Path(skill_dir) / VERSION_FILE).read_text(encoding="utf-8").strip()
        v = int(raw)
        return v if v >= 1 else 1
    except (OSError, ValueError, TypeError):
        return 1


def bump_version(skill_dir: Path) -> int:
    """版本号 +1 并写回,返回新版本号。目录不存在时抛 OSError(调用方该知道)。"""
    d = Path(skill_dir)
    new = read_version(d) + 1
    (d / VERSION_FILE).write_text(str(new), encoding="utf-8")
    return new
```

- [ ] **Step 4: 数据层补列**

`app/db/database.py`：`init_schema` 的兼容补列区（`variant` 那段之后）追加：

```python
            # 兼容旧库：skill_traces 补 skill_version 列(0=未知,历史行无从考证)
            stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)")}
            if "skill_version" not in stcols:
                conn.execute("ALTER TABLE skill_traces ADD COLUMN skill_version INTEGER DEFAULT 0")
                conn.execute("UPDATE skill_traces SET skill_version = 0 WHERE skill_version IS NULL")
                conn.commit()
```

`record_skill_trace` 签名与 INSERT 加 `skill_version: int = 0`（默认 0 = 未知，**不假装是第 1 版**）。

- [ ] **Step 5: 加载器带出版本**

`loader.py` 的 `load_skill` 返回值加 `"version": read_version(skill.path.parent)`。灰度时若服务的是候选目录（`_canary_dir`），版本取**候选目录**的——它是实际被执行的那份。

- [ ] **Step 6: 轨迹与落库串起来**

`execution_trace.py` 的 `SkillTurn` 加 `skill_version: int = 0` 字段与 `set_version(v)`；`chat.py` 里 `_preload_skill` / `load_skill` 工具调用成功后调 `set_version(result["version"])`，`_record_skill_turn` 落库时传 `skill_version=turn.skill_version`。

**不得在落库时重新读磁盘版本**——中间可能已经转正，那样记的就是错的版本。

- [ ] **Step 7: 转正时递增**

`promote_skill.py` 的 `promote()`：安装成功之后（`_replace_tree` 之后、返回之前）对 live 目录 `bump_version`。回滚（`rollback`）**不递减**——回滚出来的是一份新的 live 状态，给它一个新版本号更诚实，否则"版本 2"会指向两份不同内容。

- [ ] **Step 8: 跑测试 + 回归 + 提交**

```bash
.venv/Scripts/python.exe -m pytest tests/test_skill_versioning.py tests/test_skill_trace_db.py tests/test_skill_execution_trace.py tests/test_skills.py tests/test_promote_skill.py tests/test_promote_bundle.py tests/test_skill_canary.py tests/test_skill_watchdog.py -q
```

```bash
git add app/agent/skills/versioning.py app/agent/skills/loader.py app/agent/skills/execution_trace.py app/agent/chat.py app/db/database.py app/scripts/promote_skill.py tests/test_skill_versioning.py
git commit -m "feat(skill): 版本身份随目录走 + 轨迹记加载那一刻的版本(修复两次转正后 A/B 断链)"
```

---

## Task 3: 评测闭环——人工回复变成评测期望，回流用例进回归集

**Files:**
- Modify: `app/evaluation/trace_to_case.py`、`app/scripts/reflow_traces.py`
- Create: `app/evaluation/case_merge.py`
- Test: `tests/test_eval_loop.py`

**Interfaces:**
- Produces:
  - `trace_to_case.py`：`extract_human_reply(archived: dict) -> str`、`keywords_from_reply(reply: str, top_n: int = 5) -> list[str]`；`trace_to_case(trace, human_reply="")` 新增可选参数
  - `case_merge.py`：`merge_cases(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int]`（返回合并后列表与新增条数，按 id 去重且**不覆盖**已有）

**范围说明（对上一轮判断的更正）**：`golden_corpus.py` 已经把人工接管会话喂进 skill 蒸馏（`synthesize_skills.py:260`），这部分**不用重做**。本任务只补两件真缺的：把人工回复变成 `expected_keywords`，以及让回流用例能进回归集。

**关键约束（Global Constraint 6）**：关键词只能从**真实人工回复**里抽。抽不出就留空——`EvalCase` 的空期望在评分时被跳过而不是判 0（见 `dataset.py` 模块 docstring），所以留空是安全的，编造才危险。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_eval_loop.py`：

```python
"""评测闭环:人工回复→评测期望;回流用例→回归集(不覆盖人工维护的用例)。"""

import json

from app.evaluation.trace_to_case import (
    extract_human_reply, keywords_from_reply, trace_to_case,
)
from app.evaluation.case_merge import merge_cases
from app.agent.skills.golden_corpus import HUMAN_AGENT_INTENT


def _archived(*replies):
    msgs = []
    for intent, text in replies:
        msgs.append({"role": "user", "content": "问题"})
        msgs.append({"role": "assistant",
                     "content": json.dumps({"intent": intent, "reply": text},
                                           ensure_ascii=False)})
    return {"messages": msgs}


def test_extract_human_reply_picks_the_human_one():
    a = _archived(("product_consult", "AI 的回答"),
                  (HUMAN_AGENT_INTENT, "您的退款我已经手工加急,今天内到账"))
    assert "手工加急" in extract_human_reply(a)


def test_extract_human_reply_takes_the_last_when_several():
    a = _archived((HUMAN_AGENT_INTENT, "第一次人工回复"),
                  (HUMAN_AGENT_INTENT, "第二次人工回复才是最终答案"))
    assert "第二次" in extract_human_reply(a)


def test_extract_human_reply_empty_without_human():
    assert extract_human_reply(_archived(("product_consult", "只有 AI"))) == ""


def test_extract_tolerates_non_json_assistant():
    a = {"messages": [{"role": "assistant", "content": "纯文本旧格式"}]}
    assert extract_human_reply(a) == ""


def test_keywords_from_reply_returns_content_terms():
    kws = keywords_from_reply("您的退款我已经手工加急处理,今天24点前到账,请留意短信")
    assert kws
    assert all(len(k) >= 2 for k in kws)
    assert not any(k in ("的", "了", "我", "您") for k in kws)


def test_keywords_from_empty_reply_is_empty():
    """抽不出就留空——空期望在评分时被跳过,编造才危险。"""
    assert keywords_from_reply("") == []
    assert keywords_from_reply("好的~") == []


def test_trace_to_case_carries_human_keywords():
    trace = {"trace_id": "abcdef123", "user_input": "退款怎么还没到",
             "intent": "refund", "spans": [{"kind": "hitl"}]}
    case = trace_to_case(trace, human_reply="您的退款我已经手工加急,今天内到账")
    assert case["expected_requires_human"] is True
    assert case["expected_keywords"]


def test_trace_to_case_without_human_reply_has_no_keywords():
    """回归保护:不传人工回复时行为与改造前一致。"""
    trace = {"trace_id": "abc", "user_input": "问题", "intent": "refund", "spans": []}
    case = trace_to_case(trace)
    assert "expected_keywords" not in case or case["expected_keywords"] == []


def test_merge_adds_new_cases():
    existing = [{"id": "hand-1", "description": "人工维护"}]
    incoming = [{"id": "reflow-a", "description": "回流"}]
    merged, added = merge_cases(existing, incoming)
    assert added == 1
    assert {c["id"] for c in merged} == {"hand-1", "reflow-a"}


def test_merge_never_overwrites_existing_id():
    """人工维护的用例是资产,回流不得覆盖它。"""
    existing = [{"id": "hand-1", "description": "人工维护的原始期望"}]
    incoming = [{"id": "hand-1", "description": "回流想覆盖"}]
    merged, added = merge_cases(existing, incoming)
    assert added == 0
    assert merged[0]["description"] == "人工维护的原始期望"


def test_merge_dedupes_within_incoming():
    merged, added = merge_cases([], [{"id": "x"}, {"id": "x"}])
    assert added == 1 and len(merged) == 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest tests/test_eval_loop.py -q
```
Expected: FAIL（`extract_human_reply` / `case_merge` 不存在）

- [ ] **Step 3: 人工回复抽取与关键词**

`app/evaluation/trace_to_case.py` 追加：

```python
def extract_human_reply(archived: dict) -> str:
    """取该归档会话里**最后一条**人工坐席回复;没有则空串。

    判据与 golden_corpus 完全同源:坐席回复由 admin_session_reply 写成
    assistant 消息,intent 固定为 human_agent。取最后一条:同一会话里人工可能
    回了多轮,最终那条才是结论。

    assistant 内容非 JSON(旧格式纯文本)时跳过,不误判。
    """
    import json as _json
    from app.agent.skills.golden_corpus import HUMAN_AGENT_INTENT

    found = ""
    for msg in archived.get("messages") or []:
        if msg.get("role") != "assistant":
            continue
        try:
            data = _json.loads(str(msg.get("content") or ""))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("intent") == HUMAN_AGENT_INTENT:
            found = str(data.get("reply") or "")
    return found


# 中文停用词/客套词:出现在几乎每条回复里,当期望关键词等于不设期望
_STOPWORDS = {
    "的", "了", "我", "您", "你", "是", "在", "有", "和", "就", "都", "会",
    "请", "好的", "麻烦", "稍等", "感谢", "抱歉", "不好意思", "亲",
}
_MIN_KEYWORD_LEN = 2


def keywords_from_reply(reply: str, top_n: int = 5) -> list[str]:
    """从人工回复里抽几个内容词,作为评测的 expected_keywords。

    抽不出就返回 []——EvalCase 的空期望在评分时**被跳过而不是判 0**
    (见 dataset.py 模块 docstring),所以留空安全,编造才危险。

    刻意用确定性切分而不是 LLM:评测期望必须可复现,且不该每次回流都花钱。
    """
    import re

    text = (reply or "").strip()
    if not text:
        return []
    # 按标点与空白切成短语,再滤掉停用词与过短片段
    parts = [p.strip() for p in re.split(r"[，。,.;；:：!！?？\s~、]+", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if len(p) < _MIN_KEYWORD_LEN or p in _STOPWORDS:
            continue
        if any(p == o or p in o for o in out):
            continue
        out.append(p)
        if len(out) >= max(1, int(top_n)):
            break
    return out
```

`trace_to_case` 签名改为 `def trace_to_case(trace: dict, human_reply: str = "") -> dict:`，在设完 `expected_requires_human` 之后追加：

```python
    if human_reply:
        kws = keywords_from_reply(human_reply)
        if kws:
            case["expected_keywords"] = kws
```

- [ ] **Step 4: 实现 `app/evaluation/case_merge.py`**

```python
"""回流用例合并进回归集:按 id 去重,**永不覆盖**已有用例。

人工维护的用例带着人写的期望,是资产;回流用例是机器从线上 trace 生成的推测。
后者不该覆盖前者——否则一次回流就能把人调好的期望冲掉。
"""

from __future__ import annotations


def merge_cases(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int]:
    """返回 (合并后列表, 新增条数)。已存在的 id 原样保留,不合并字段。"""
    seen = {str(c.get("id")) for c in existing if c.get("id")}
    merged = list(existing)
    added = 0
    for c in incoming:
        cid = str(c.get("id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        merged.append(c)
        added += 1
    return merged, added
```

- [ ] **Step 5: CLI 支持合并进回归集**

`app/scripts/reflow_traces.py` 加 `--merge-into <path>`（默认不合并，保持现有"只写独立文件"的行为）。合并时读目标 JSON 的 `{"cases": [...]}`，调 `merge_cases`，原子写回，并打印新增条数。**不传该参数时行为完全不变**。

- [ ] **Step 6: 跑测试 + 回归 + 提交**

```bash
.venv/Scripts/python.exe -m pytest tests/test_eval_loop.py tests/test_golden_corpus.py tests/test_eval_skill_filter.py -q
```

```bash
git add app/evaluation/trace_to_case.py app/evaluation/case_merge.py app/scripts/reflow_traces.py tests/test_eval_loop.py
git commit -m "feat(eval): 人工回复变成评测期望 + 回流用例可合并进回归集(不覆盖人工用例)"
```

---

## 端到端验收（三块都实施完后实跑）

1. 把某买家会话置人工接管，批准一条指向他的草稿 → 拒绝并给出中文原因，草稿仍留待审。
2. 解除接管后再批准 → 正常发出。
3. 制造未结工单 → 同样拒绝；结掉工单 → 放行。
4. 连续转正同一 skill 两次，查 `skill_traces` 能按 `skill_version` 把两批轨迹分开。
5. 回滚后再看版本号：是一个**新**版本号，不是回到旧号。
6. 一条含人工回复的归档会话回流 → 生成的用例带 `expected_keywords`，内容来自人工原话。
7. `--merge-into app/evaluation/cases.json` 跑两次 → 第二次新增 0 条，人工用例的 description 未被改动。
8. 全量 pytest 失败集合与基线（16 failed / 4 errors）逐条一致。

---

## Self-Review

**规格覆盖**：① 仲裁（Task 1，含端点接入与 fail-closed）；④ 版本身份（Task 2，含"记加载那一刻"这条约束与回滚语义）；⑤ 评测闭环（Task 3，已按核实结果缩小到真缺的两件）。

**占位符扫描**：Task 2 的 Step 5/6/7 是"改哪个文件的哪个位置 + 约束"而非整段代码——因为这三处都是往既有函数里插一两行，贴整函数反而会和实际代码脱节。约束（不得落库时重读磁盘、回滚不递减）已写明，测试是验收标准。其余均为可直接落地的完整实现。

**类型一致性**：`skill_version` 在 `record_skill_trace` / `SkillTurn` / `load_skill` 返回值三处同名同型（int，0=未知）；`check_outreach_allowed` 的 `(bool, str)` 返回在模块与端点两处一致；`merge_cases` 的 `(list, int)` 返回在模块与 CLI 两处一致。

**已知边界（写进代码注释，不留给读者猜）**：`ManualMode` 内存态重启即丢，仲裁此时只剩工单这一半依据。
