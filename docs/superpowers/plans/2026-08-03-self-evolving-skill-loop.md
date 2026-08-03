# 自进化 Skill 闭环（企业级对齐）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有「离线产候选、无校验无门禁」的半成品自进化 Skill，补成企业级**在线自进化**闭环：执行轨迹落库 → 按真实轨迹采集失败案例 → 从人工接管语料蒸馏 → 候选过工具校验 → **按风险分级授权** → 低危灰度 A/B 自动转正、劣化自动回滚，高危（碰钱/承诺类）永远人工确认。

**Architecture:** 四层叠加，不动 ReAct 主逻辑。**数据层**新增 `skill_traces` 表记录「每轮加载了哪个 skill / 哪个版本 / 调了哪些工具 / 结局如何」；**蒸馏层**把采样源从「首条消息关键词猜测」换成「真实执行轨迹 + 人工接管语料」；**门禁层**做静态工具校验 + 影子目录离线评测；**授权层**按候选引用的工具与措辞自动判风险档，低危走灰度 A/B（按会话哈希分流，看门狗按实战成功率自动转正或回滚），高危一律人工。

**Tech Stack:** Python 3.11 / SQLite（`app/db/database.py`）/ pytest / OpenAI SDK（已有）/ PyYAML（已有）。不引入任何新第三方依赖。

## Global Constraints

- **分级授权铁律**（本计划的核心安全约束）：写入 `definitions/` 正式目录**只允许**发生在 `app/scripts/promote_skill.py::promote` 一个函数里；其余任何流程只能写 `_candidates/`。放行凭据按风险档分三种：
  - `high`（引用 `apply_refund`/`cancel_order`/`change_address`/`negotiate_price`/`issue_invoice`/`expedite_shipping`，或正文含承诺类措辞）→ **必须人工执行 CLI**，`skill_watchdog` 等任何自动代码路径不得调用 `promote`。
  - `medium`（全新 skill）→ 允许自动转正，但必须先过离线评测门禁；转正后由绝对成功率看门狗守着。
  - `low`（改进已有 skill 且只用只读工具）→ 允许灰度 A/B 胜出后自动转正（`force=True`：实战数据是比离线评测更强的证据）。
  - **任何档位、任何 force 都不得跳过 `validate_candidate`**——编造工具名的候选永远不许上线。
- **SkillManager 加载边界**：`loader.SkillManager._discover` 只扫 `skills_dir` 的**直接子目录**下的 `SKILL.md`。`_candidates/<name>/SKILL.md`、`_archive/<name>/<ts>/SKILL.md`、`_shadow/` 都多嵌一层或以 `_` 开头，因此不会被加载。新增目录必须保持这个嵌套层级。灰度期候选正文由 `load_skill` 运行时替换，**不落进 `definitions/`**。
- **灰度必须 fail-soft 且会话内一致**：灰度路由的任何异常（开关关闭、取不到会话、DB 故障、候选文件缺失）都退回正式版本；分桶用 `md5(skill_name:session_id)` 确定性哈希，禁止用随机数——同一通对话中途换版本会让顾客被两套流程处理。
- **fail-soft**：LLM 坏输出、坏 JSON、缺文件一律跳过并返回空值/None，绝不抛异常拖垮离线脚本或请求链路。
- **G2 只观察不否决**：执行轨迹记录是旁路埋点，任何异常必须被吞掉，绝不影响 `chat()` 的回复结果。
- **DB 兼容旧库**：新表用 `CREATE TABLE IF NOT EXISTS`，写在 `init_schema` 的 `executescript` 里，不做破坏性迁移。
- **门禁 fail-closed**：没有可用评测用例时，`gate_candidate` 返回 `promote=False`，不得默认放行。
- **代码风格**：中文 docstring/注释，与现有文件一致；跨平台用 `pathlib.Path`，不硬编码路径分隔符。
- **测试命令统一**：`.venv/Scripts/python.exe -m pytest <file> -v`（Git Bash 下可用）。

## 范围说明（必读）

本计划覆盖 Part A 的 **G2 / G3 / G4 / G5 / G6**，外加**分级授权的在线自进化**（Task 12–15）——即「自进化闭环」这一个子系统。

任务分两段，**Task 1–11 是 Task 12–15 的前提**（没有 `skill_traces` 的实战数据，灰度与看门狗无从判断，不要提前做）：
- **第一段（Task 1–11）**：闭环本体 —— 轨迹落库、蒸馏源升级、工具校验、离线门禁、备份转正/回滚、管理端总览。此时全部候选都需人工确认转正。
- **第二段（Task 12–15）**：分级授权 —— 风险自动判档、灰度按会话分流、看门狗自动转正/回滚。做完后低危候选真正做到无人值守上线，高危仍锁人工。

**G1（Skill 从文本升级为结构化工作流）不在本计划内**：它改的是 skill 的*执行模型*（`load_skill` 返回文本 → 返回可执行步骤 schema），会动到 `chat.py` 的 ReAct 主路径与 `app/agent/strategies/`（目前只有一个空 docstring，无实现），属于独立子系统。应在本计划落地、闭环稳定后单独出一份计划。本计划的产物对 G1 无阻塞：`skill_traces` 正好是 G1 做分支覆盖率统计的数据基础。

## File Structure

**新建**
| 文件 | 职责 |
|---|---|
| `app/agent/skills/execution_trace.py` | `SkillTurn` 数据结构：本轮加载的 skill、工具调用明细、结局判定。纯逻辑，无 IO |
| `app/agent/skills/validator.py` | 候选 SKILL.md 静态校验：frontmatter 完整性 + 引用的工具名是否真实存在（G6） |
| `app/agent/skills/failure_cases.py` | 按 `skill_traces` 真实轨迹归集每个 skill 的失败会话样本（G3） |
| `app/agent/skills/golden_corpus.py` | 从归档会话中识别人工接管语料并抽取样本（G5） |
| `app/agent/skills/gate.py` | 影子目录构建 + 候选灰度评测门禁（G4） |
| `app/scripts/promote_skill.py` | 转正/回滚/列表 CLI：校验 → 门禁 → 备份 → 写正式目录 |
| `tests/test_skill_execution_trace.py` | `SkillTurn` 单测 |
| `tests/test_skill_trace_db.py` | `skill_traces` 表读写单测 |
| `tests/test_skill_validator.py` | 校验器单测 |
| `tests/test_skill_failure_cases.py` | 失败样本归集单测 |
| `tests/test_golden_corpus.py` | 人工语料识别单测 |
| `tests/test_skill_gate.py` | 门禁单测（注入 fake eval_fn，不触网） |
| `tests/test_promote_skill.py` | 转正/回滚 CLI 单测 |

**修改**
| 文件 | 改动 |
|---|---|
| `app/db/database.py` | `init_schema` 加 `skill_traces` 表；新增 `record_skill_trace` / `list_skill_traces` |
| `app/config/settings.py:74-84` | 加 `skill_trace_enabled` / `skill_gate_tolerance` |
| `app/agent/chat.py:132-136, 191, 335-336` | 三处埋点：回合开始重置、工具调用后记录、回合结束落库 |
| `app/agent/skills/synthesizer.py` | `synthesize_one` / `synthesize_skills` 支持注入 system prompt 与真实工具清单；产物过校验 |
| `app/evaluation/dataset.py` | `EvalCase` 加 `related_skills` 字段；新增 `filter_by_skill` |
| `app/evaluation/cases.json` | 给 3 条用例打 `related_skills` 标签 |
| `app/scripts/synthesize_skills.py` | 闭环入口接入 trace 失败源 + 金牌语料 + 校验摘要 |
| `app/api/app.py` | 新增 `GET /api/admin/skills/candidates` |
| `.env.example` | 文档化两个新开关 |

---

### Task 1: skill_traces 表与读写方法（G2 数据层）

**Files:**
- Modify: `app/db/database.py:105-114`（`init_schema` 的 executescript 尾部）
- Modify: `app/db/database.py:296-315`（`list_recent_archives` 之后追加新方法）
- Test: `tests/test_skill_trace_db.py`

**Interfaces:**
- Consumes: 无（本任务是闭环的最底层）
- Produces:
  - `Database.record_skill_trace(session_id: str, user_id: str, skill_name: str, tool_calls: list[dict], outcome: str) -> None`
  - `Database.list_skill_traces(skill_name: str | None = None, outcomes: list[str] | None = None, limit: int = 200) -> list[dict]`
    返回的每个 dict 含 `id / session_id / user_id / skill_name / tool_calls(list) / outcome / created_at`；`tool_calls` 已 `json.loads`，坏 JSON 的行被跳过。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_trace_db.py`：

```python
"""G2 数据层：skill_traces 表读写（记录每轮 skill 执行轨迹，供失败采集/门禁分析）。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_record_and_list_skill_trace(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace(
        "s1", "u1", "process-return",
        [{"name": "query_order", "ok": True, "error": None}],
        "success",
    )

    rows = db.list_skill_traces()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u1"
    assert row["skill_name"] == "process-return"
    assert row["outcome"] == "success"
    assert row["tool_calls"] == [{"name": "query_order", "ok": True, "error": None}]
    assert row["created_at"]


def test_list_skill_traces_filters_by_skill_and_outcome(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace("s1", "u1", "process-return", [], "success")
    db.record_skill_trace("s2", "u1", "process-return", [], "handoff")
    db.record_skill_trace("s3", "u2", "track-order", [], "handoff")

    only_return = db.list_skill_traces(skill_name="process-return")
    assert {r["session_id"] for r in only_return} == {"s1", "s2"}

    failed_return = db.list_skill_traces(
        skill_name="process-return", outcomes=["handoff", "tool_error"]
    )
    assert [r["session_id"] for r in failed_return] == ["s2"]

    all_failed = db.list_skill_traces(outcomes=["handoff"])
    assert {r["session_id"] for r in all_failed} == {"s2", "s3"}


def test_list_skill_traces_newest_first_and_respects_limit(tmp_path):
    db = _db(tmp_path)
    for i in range(3):
        db.record_skill_trace(f"s{i}", "u1", "track-order", [], "success")

    rows = db.list_skill_traces(limit=2)
    assert [r["session_id"] for r in rows] == ["s2", "s1"]   # id DESC


def test_list_skill_traces_skips_bad_json(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace("good", "u1", "track-order", [{"name": "x", "ok": True, "error": None}], "success")

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
            "outcome, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("bad", "u1", "track-order", "{not json", "success", "2026-01-01 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    rows = db.list_skill_traces()
    assert [r["session_id"] for r in rows] == ["good"]   # 坏 JSON 那条被跳过
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_trace_db.py -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'record_skill_trace'`

- [ ] **Step 3: 加建表语句**

在 `app/db/database.py` 的 `init_schema` 里，把 `CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, status);` 那一行之后、`"""` 之前，插入：

```sql
                CREATE TABLE IF NOT EXISTS skill_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT,
                    skill_name TEXT NOT NULL,
                    tool_calls TEXT,
                    outcome TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_skill_traces_name
                    ON skill_traces(skill_name, id);
```

- [ ] **Step 4: 加读写方法**

在 `app/db/database.py` 的 `list_recent_archives` 方法之后（`finally: conn.close()` 结束后）追加：

```python
    # ---------- Skill 执行轨迹(G2:每轮"加载了哪个 skill/调了哪些工具/结局如何") ----------
    def record_skill_trace(self, session_id: str, user_id: str, skill_name: str,
                           tool_calls: list[dict], outcome: str) -> None:
        """记录一轮 skill 执行轨迹。供 G3 按真实轨迹采集失败案例、G4 门禁分析。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
                "outcome, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user_id, skill_name,
                 json.dumps(tool_calls or [], ensure_ascii=False), outcome, self._now()),
            )
            conn.commit()
        finally:
            conn.close()

    def list_skill_traces(self, skill_name: Optional[str] = None,
                          outcomes: Optional[list[str]] = None,
                          limit: int = 200) -> list[dict]:
        """按 id DESC 取轨迹;可按 skill 名与结局过滤。tool_calls 反序列化成 list,
        坏 JSON 的行跳过(与 list_recent_archives 同口径,单条脏数据不崩离线脚本)。"""
        sql = "SELECT * FROM skill_traces"
        clauses: list[str] = []
        params: list = []
        if skill_name:
            clauses.append("skill_name = ?")
            params.append(skill_name)
        if outcomes:
            clauses.append(f"outcome IN ({','.join('?' * len(outcomes))})")
            params.extend(outcomes)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["tool_calls"] = json.loads(item["tool_calls"]) if item["tool_calls"] else []
                except (json.JSONDecodeError, TypeError):
                    continue
                results.append(item)
            return results
        finally:
            conn.close()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_trace_db.py -v`
Expected: PASS（4 passed）

- [ ] **Step 6: 跑既有 DB 测试确认没破坏兼容**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db_schema.py tests/test_db_write.py tests/test_skill_synth.py -v`
Expected: PASS（全绿）

- [ ] **Step 7: 提交**

```bash
git add app/db/database.py tests/test_skill_trace_db.py
git commit -m "feat(skill): 新增 skill_traces 表与读写方法(自进化闭环数据层)"
```

---

### Task 2: 执行轨迹采集接入运行链路（G2 埋点）

**Files:**
- Create: `app/agent/skills/execution_trace.py`
- Modify: `app/config/settings.py:74-77`
- Modify: `app/agent/chat.py:132-136`（回合开始）、`app/agent/chat.py:335-336`（工具后）、`app/agent/chat.py:188-191`（回合结束）
- Modify: `.env.example`
- Test: `tests/test_skill_execution_trace.py`

**Interfaces:**
- Consumes: `Database.record_skill_trace(...)`（Task 1）
- Produces:
  - `app.agent.skills.execution_trace.SkillTurn` 数据类，字段 `skill_name: str`、`tool_calls: list[dict]`
  - `SkillTurn.note_tool_call(name: str, result_str: str) -> None`
  - `SkillTurn.outcome(requires_human: bool) -> str`（返回 `"success"` / `"tool_error"` / `"handoff"`）
  - `SkillTurn.has_skill -> bool`
  - 常量 `OUTCOME_SUCCESS` / `OUTCOME_TOOL_ERROR` / `OUTCOME_HANDOFF` / `LOAD_SKILL_TOOL`
  - `settings.skill_trace_enabled: bool`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_execution_trace.py`：

```python
"""G2 埋点纯逻辑:SkillTurn 从工具调用结果推断加载的 skill 与本轮结局。"""

import json

from app.agent.skills.execution_trace import (
    OUTCOME_HANDOFF,
    OUTCOME_SUCCESS,
    OUTCOME_TOOL_ERROR,
    SkillTurn,
)


def _load_skill_ok(name="process-return"):
    return json.dumps({"success": True, "skill_name": name, "instructions": "步骤..."},
                      ensure_ascii=False)


def test_fresh_turn_has_no_skill():
    turn = SkillTurn()
    assert turn.has_skill is False
    assert turn.tool_calls == []


def test_load_skill_success_records_skill_name():
    turn = SkillTurn()
    turn.note_tool_call("load_skill", _load_skill_ok())
    assert turn.skill_name == "process-return"
    assert turn.has_skill is True


def test_load_skill_failure_does_not_record_skill_name():
    turn = SkillTurn()
    turn.note_tool_call("load_skill", json.dumps({"success": False, "error": "未找到技能"},
                                                 ensure_ascii=False))
    assert turn.skill_name == ""
    assert turn.has_skill is False
    assert turn.tool_calls[0]["ok"] is False


def test_tool_call_ok_and_error_flags():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True, "order": {}}, ensure_ascii=False))
    turn.note_tool_call("apply_refund", json.dumps({"success": False, "error": "订单不存在"},
                                                   ensure_ascii=False))
    turn.note_tool_call("query_product", json.dumps({"error": "工具执行出错: boom"}, ensure_ascii=False))

    assert turn.tool_calls[0] == {"name": "query_order", "ok": True, "error": None}
    assert turn.tool_calls[1] == {"name": "apply_refund", "ok": False, "error": "订单不存在"}
    assert turn.tool_calls[2]["ok"] is False


def test_non_json_result_treated_as_ok():
    """工具结果不是 JSON 时无法判定成败,按成功计(不制造假失败)。"""
    turn = SkillTurn()
    turn.note_tool_call("search_knowledge", "一段纯文本检索结果")
    assert turn.tool_calls[0]["ok"] is True


def test_outcome_handoff_wins_over_tool_error():
    turn = SkillTurn()
    turn.note_tool_call("apply_refund", json.dumps({"success": False, "error": "x"}, ensure_ascii=False))
    assert turn.outcome(requires_human=True) == OUTCOME_HANDOFF


def test_outcome_tool_error_when_any_tool_failed():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))
    turn.note_tool_call("apply_refund", json.dumps({"success": False, "error": "x"}, ensure_ascii=False))
    assert turn.outcome(requires_human=False) == OUTCOME_TOOL_ERROR


def test_outcome_success_when_all_tools_ok():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))
    assert turn.outcome(requires_human=False) == OUTCOME_SUCCESS
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_execution_trace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.execution_trace'`

- [ ] **Step 3: 实现 execution_trace.py**

创建 `app/agent/skills/execution_trace.py`：

```python
"""G2 Skill 执行轨迹:记录每轮"加载了哪个 skill / 调了哪些工具 / 结局如何"。

用途:让自进化的采样源从"猜首条消息关键词"升级为"读真实执行轨迹"——
G3 按 outcome 采失败案例、G4 门禁按 skill 维度看成功率。

本模块是纯逻辑(无 IO、无 settings 依赖),落库由调用方(chat.py 回合结束)完成。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

LOAD_SKILL_TOOL = "load_skill"

OUTCOME_SUCCESS = "success"
OUTCOME_TOOL_ERROR = "tool_error"
OUTCOME_HANDOFF = "handoff"


def _parse_result(result_str: str) -> tuple[bool, str | None]:
    """从工具返回的 JSON 串判定成败。

    - `{"success": false, ...}` / 含 `error` 字段 → 失败,带错误文案。
    - 非 JSON / 无这两个字段 → 无法判定,按成功计(不制造假失败)。
    """
    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return True, None
    if not isinstance(data, dict):
        return True, None

    if data.get("success") is False:
        return False, str(data.get("error") or data.get("message") or "")
    if data.get("error"):
        return False, str(data["error"])
    return True, None


def _loaded_skill_name(result_str: str) -> str:
    """从 load_skill 的成功返回里取出 skill 名;取不到返回空串。"""
    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("skill_name") or "").strip()


@dataclass
class SkillTurn:
    """单轮对话的 skill 执行轨迹。未加载 skill 的轮次不会被落库(has_skill=False)。"""

    skill_name: str = ""
    tool_calls: list[dict] = field(default_factory=list)

    def note_tool_call(self, name: str, result_str: str) -> None:
        """记录一次工具调用。若是成功的 load_skill,同时记下本轮加载的 skill 名。"""
        ok, error = _parse_result(result_str)
        if name == LOAD_SKILL_TOOL and ok:
            loaded = _loaded_skill_name(result_str)
            if loaded:
                self.skill_name = loaded
        self.tool_calls.append({"name": name, "ok": ok, "error": error})

    def outcome(self, requires_human: bool) -> str:
        """本轮结局:转人工 > 工具失败 > 成功(转人工是更强的负信号,优先)。"""
        if requires_human:
            return OUTCOME_HANDOFF
        if any(not call["ok"] for call in self.tool_calls):
            return OUTCOME_TOOL_ERROR
        return OUTCOME_SUCCESS

    @property
    def has_skill(self) -> bool:
        return bool(self.skill_name)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_execution_trace.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 加配置开关**

在 `app/config/settings.py` 的 `skill_synth_enabled: bool = False` 那一行之后追加：

```python
    skill_trace_enabled: bool = True   # G2:记录每轮 skill 执行轨迹(旁路埋点,异常不影响回复)
    skill_gate_tolerance: float = 0.05  # G4:候选灰度评测允许的最大掉点,超过即拒绝转正
```

在 `.env.example` 的 `MULTI_AGENT_ENABLED=false` 段落之后追加：

```
# 自进化 Skill 闭环:记录 skill 执行轨迹(供失败采集/门禁);候选灰度评测容差
SKILL_TRACE_ENABLED=true
SKILL_GATE_TOLERANCE=0.05
```

- [ ] **Step 6: chat.py 埋点一 —— 回合开始重置**

在 `app/agent/chat.py` 的 `chat()` 里，把这一段：

```python
        self._turn_recall = None   # 新一轮:召回缓存作废,按本轮问题重检索
        self._turn_item_ctx = None
```

改为：

```python
        self._turn_recall = None   # 新一轮:召回缓存作废,按本轮问题重检索
        self._turn_item_ctx = None
        from app.agent.skills.execution_trace import SkillTurn
        self._skill_turn = SkillTurn()   # G2:新一轮 skill 执行轨迹(旁路埋点)
```

- [ ] **Step 7: chat.py 埋点二 —— 工具调用后记录**

在 `app/agent/chat.py` 的 `_execute_tool_call` 里，把这一段：

```python
        result_str = self.tool_manager.execute_tool(name, args)
        self._emit({"type": "tool_result", "content": result_str})             # after
```

改为：

```python
        result_str = self.tool_manager.execute_tool(name, args)
        self._emit({"type": "tool_result", "content": result_str})             # after

        # G2 旁路埋点:记进本轮 skill 轨迹。只观察不否决,异常一律吞掉。
        turn = getattr(self, "_skill_turn", None)
        if turn is not None:
            try:
                turn.note_tool_call(name, result_str)
            except Exception:  # noqa: BLE001
                pass
```

- [ ] **Step 8: chat.py 埋点三 —— 回合结束落库**

在 `app/agent/chat.py` 的 `chat()` 里，把结尾这一段：

```python
        self._status = "complete"
        self.store.save(self.session_path, self._session_state())   # 回合结束:完整落盘(必落)
        self._write_snapshot()
        return result
```

改为：

```python
        self._status = "complete"
        self.store.save(self.session_path, self._session_state())   # 回合结束:完整落盘(必落)
        self._write_snapshot()
        self._record_skill_turn(result)
        return result

    def _record_skill_turn(self, result: CustomerServiceResponse) -> None:
        """G2:本轮若加载过 skill,把执行轨迹落库(供 G3 失败采集 / G4 门禁分析)。

        旁路埋点:开关关闭、未加载 skill、或落库失败都直接返回,绝不影响回复。
        """
        if not settings.skill_trace_enabled:
            return
        turn = getattr(self, "_skill_turn", None)
        if turn is None or not turn.has_skill:
            return
        try:
            from app.db import get_db
            get_db().record_skill_trace(
                session_id=self.session_id, user_id=self.user_id,
                skill_name=turn.skill_name, tool_calls=turn.tool_calls,
                outcome=turn.outcome(result.requires_human),
            )
        except Exception:  # noqa: BLE001 埋点失败绝不影响本轮回复
            pass
```

> 说明：FAQ 缓存秒答路径（`chat()` 中提前 `return result` 那一支）在 ReAct 之前返回，不可能加载过 skill，故无需埋点。

- [ ] **Step 9: 写埋点接线测试**

在 `tests/test_skill_execution_trace.py` 末尾追加：

```python
# ---------- 埋点接线:回合结束落库(不跑真 LLM,直接调 _record_skill_turn) ----------

def test_record_skill_turn_writes_trace(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database
    from app.schemas.response import CustomerServiceResponse, IntentType

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_trace_enabled", True)

    agent = EcomAgent.__new__(EcomAgent)          # 不跑 __init__,只测埋点方法
    agent.session_id = "s-trace"
    agent.user_id = "u-trace"
    agent._skill_turn = SkillTurn()
    agent._skill_turn.note_tool_call("load_skill", _load_skill_ok("track-order"))
    agent._skill_turn.note_tool_call("query_logistics",
                                     json.dumps({"success": True}, ensure_ascii=False))

    result = CustomerServiceResponse(intent=IntentType.OTHER, confidence=1.0,
                                     reply="已查到物流", requires_human=False,
                                     follow_up_question=None)
    agent._record_skill_turn(result)

    rows = db.list_skill_traces()
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "track-order"
    assert rows[0]["outcome"] == "success"
    assert [c["name"] for c in rows[0]["tool_calls"]] == ["load_skill", "query_logistics"]


def test_record_skill_turn_noop_without_skill(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database
    from app.schemas.response import CustomerServiceResponse, IntentType

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_trace_enabled", True)

    agent = EcomAgent.__new__(EcomAgent)
    agent.session_id = "s-none"
    agent.user_id = "u-none"
    agent._skill_turn = SkillTurn()
    agent._skill_turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))

    agent._record_skill_turn(CustomerServiceResponse(
        intent=IntentType.OTHER, confidence=1.0, reply="ok",
        requires_human=False, follow_up_question=None))

    assert db.list_skill_traces() == []   # 未加载 skill 的轮次不落库
```

- [ ] **Step 10: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_execution_trace.py -v`
Expected: PASS（10 passed）

- [ ] **Step 11: 回归既有 agent 测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_skills.py tests/test_checkpoint.py -v`
Expected: PASS（全绿，埋点不改变既有行为）

- [ ] **Step 12: 提交**

```bash
git add app/agent/skills/execution_trace.py app/agent/chat.py app/config/settings.py .env.example tests/test_skill_execution_trace.py
git commit -m "feat(skill): 采集 skill 执行轨迹并落库(旁路埋点,不影响回复)"
```

---

### Task 3: 候选静态校验器（G6）

**Files:**
- Create: `app/agent/skills/validator.py`
- Test: `tests/test_skill_validator.py`

**Interfaces:**
- Consumes: `app.agent.skills.loader._parse_frontmatter`、`app.agent.tools.registry.TOOL_DEFINITIONS`
- Produces:
  - `known_tool_names() -> set[str]`
  - `referenced_tools(content: str) -> set[str]`
  - `validate_candidate(content: str, known: set[str] | None = None) -> dict`
    返回 `{"valid": bool, "name": str, "description": str, "unknown_tools": list[str], "errors": list[str]}`；`unknown_tools` 已排序。

> **为什么必须有这一关**：实测离线合成产出的候选把工具名编错了——写 `order_list`（真实是 `list_user_orders`）、`coupon_query`（真实是 `query_coupons`）。若直接转正，Agent 会照指令去调不存在的工具而失败。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_validator.py`：

```python
"""G6 候选校验:frontmatter 完整性 + 引用的工具名必须真实存在于 registry。

实测背景:LLM 合成的候选写过 `order_list`(真实是 list_user_orders)、
`coupon_query`(真实是 query_coupons),直接转正会让 Agent 调到不存在的工具。
"""

from app.agent.skills.validator import known_tool_names, referenced_tools, validate_candidate

GOOD = """---
name: order-query-all
description: 用户请求查询名下全部订单时触发。
---
第一步：调用 `list_user_orders` 查询用户名下全部订单。
第二步：需要单笔明细时调用 `query_order`。
"""

BAD_TOOL = """---
name: order-query-all
description: 用户请求查询名下全部订单时触发。
---
第一步：调用 `order_list` 查询用户名下全部订单。
第二步：优惠券用 `coupon_query` 查询。
"""

NO_FRONTMATTER = "这是一段没有 frontmatter 的纯文本。"

MISSING_DESC = """---
name: order-query-all
---
正文。
"""


def test_known_tool_names_contains_real_tools():
    names = known_tool_names()
    assert "list_user_orders" in names
    assert "query_coupons" in names
    assert "load_skill" in names
    assert "order_list" not in names


def test_referenced_tools_extracts_backticked_snake_case():
    refs = referenced_tools(GOOD)
    assert refs == {"list_user_orders", "query_order"}


def test_referenced_tools_ignores_prose_and_non_identifiers():
    content = "第一步：先确认订单，再走 `七天无理由` 流程，参考 `Nike Air` 商品。"
    assert referenced_tools(content) == set()


def test_validate_good_candidate_passes():
    result = validate_candidate(GOOD)
    assert result["valid"] is True
    assert result["name"] == "order-query-all"
    assert result["description"]
    assert result["unknown_tools"] == []
    assert result["errors"] == []


def test_validate_flags_unknown_tools():
    result = validate_candidate(BAD_TOOL)
    assert result["valid"] is False
    assert result["unknown_tools"] == ["coupon_query", "order_list"]   # 已排序
    assert any("未知工具" in e for e in result["errors"])


def test_validate_rejects_missing_frontmatter():
    result = validate_candidate(NO_FRONTMATTER)
    assert result["valid"] is False
    assert result["name"] == ""
    assert any("frontmatter" in e for e in result["errors"])


def test_validate_rejects_missing_description():
    result = validate_candidate(MISSING_DESC)
    assert result["valid"] is False
    assert any("description" in e for e in result["errors"])


def test_validate_accepts_injected_known_set():
    """允许注入工具清单,便于单测与未来多套工具集。"""
    result = validate_candidate(BAD_TOOL, known={"order_list", "coupon_query"})
    assert result["valid"] is True
    assert result["unknown_tools"] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_validator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.validator'`

- [ ] **Step 3: 实现 validator.py**

创建 `app/agent/skills/validator.py`：

```python
"""G6 候选 Skill 静态校验:frontmatter 完整性 + 工具名是否真实存在。

为什么需要:LLM 合成候选时会凭空编工具名(实测产出过 `order_list`、
`coupon_query`,而真实工具是 `list_user_orders`、`query_coupons`)。这类候选一旦
转正,Agent 会照指令调不存在的工具直接失败——所以转正前必须过这一关。

工具引用的识别口径:只认反引号内的 snake_case 标识符(``query_order``)。
业务散文、中文短语、含空格的商品名都不会被误判成工具名。
"""

from __future__ import annotations

import re

from app.agent.skills.loader import _parse_frontmatter

# 反引号内的 snake_case 标识符:小写字母开头,允许下划线与数字
_INLINE_CODE_RE = re.compile(r"`([a-z][a-z0-9_]*)`")


def known_tool_names() -> set[str]:
    """registry 中真实注册的工具名全集(含按开关动态追加的工具)。"""
    from app.agent.tools.registry import TOOL_DEFINITIONS

    names = set()
    for item in TOOL_DEFINITIONS:
        name = (item.get("function") or {}).get("name")
        if name:
            names.add(str(name))
    return names


def referenced_tools(content: str) -> set[str]:
    """抽取 SKILL.md 里引用的工具名候选(反引号内的 snake_case 标识符)。

    只取含下划线的、或已知工具名单形态的标识符,避免把 `body`、`sku` 这类
    普通单词当成工具引用。
    """
    refs = set()
    for token in _INLINE_CODE_RE.findall(content or ""):
        if "_" in token:
            refs.add(token)
    return refs


def validate_candidate(content: str, known: set[str] | None = None) -> dict:
    """校验一份候选 SKILL.md 文本。

    返回 `{"valid", "name", "description", "unknown_tools", "errors"}`:
    - frontmatter 缺失/无 name/无 description → valid=False;
    - 引用了 registry 里不存在的工具 → valid=False,unknown_tools 列出(已排序);
    - 全部通过 → valid=True。
    """
    errors: list[str] = []

    meta = _parse_frontmatter(content)
    if not meta:
        errors.append("缺少合法的 YAML frontmatter(必须以 --- 开头并包含 name/description)")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name:
        errors.append("frontmatter 缺少 name")
    if not description:
        errors.append("frontmatter 缺少 description")

    known_set = known if known is not None else known_tool_names()
    unknown = sorted(referenced_tools(content) - known_set)
    if unknown:
        errors.append(f"引用了未知工具: {', '.join(unknown)}")

    return {
        "valid": not errors,
        "name": name,
        "description": description,
        "unknown_tools": unknown,
        "errors": errors,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_validator.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 用真实候选验证（人工确认这一关确实抓到了实测缺陷）**

Run:
```bash
.venv/Scripts/python.exe -c "
from pathlib import Path
from app.agent.skills.validator import validate_candidate
root = Path('app/agent/skills/definitions/_candidates')
for p in sorted(root.glob('*/SKILL.md')) if root.exists() else []:
    r = validate_candidate(p.read_text(encoding='utf-8'))
    print(p.parent.name, '->', 'OK' if r['valid'] else r['errors'])
"
```
Expected: 若上次离线合成的候选还在，应看到 `order-query-all -> ["引用了未知工具: order_list"]`、`coupon-lookup -> ["引用了未知工具: coupon_query"]` 之类输出；候选目录为空则无输出（不算失败）。

- [ ] **Step 6: 提交**

```bash
git add app/agent/skills/validator.py tests/test_skill_validator.py
git commit -m "feat(skill): 候选静态校验器(frontmatter + 工具名真实性)"
```

---

### Task 4: 合成器注入真实工具清单并强制过校验（G6 接入）

**Files:**
- Modify: `app/agent/skills/synthesizer.py:46-60`（`SYNTH_SYSTEM_PROMPT`）、`app/agent/skills/synthesizer.py:131-182`（`synthesize_one` / `synthesize_skills`）、`app/agent/skills/synthesizer.py:206-248`（`improve_skill`）
- Test: `tests/test_skill_synth_validation.py`

**Interfaces:**
- Consumes: `validate_candidate(content, known)`（Task 3）、`known_tool_names()`
- Produces（签名变更，向后兼容，旧调用方不传新参数即保持原行为语义）:
  - `build_tool_hint(known: set[str] | None = None) -> str`
  - `synthesize_one(client, model, group, system_prompt: str = SYNTH_SYSTEM_PROMPT, known_tools: set[str] | None = None) -> dict | None`
  - `synthesize_skills(client, model, samples, out_dir, system_prompt: str = SYNTH_SYSTEM_PROMPT, known_tools: set[str] | None = None) -> list[Path]`
  - `improve_skill(client, model, skill, failure_cases, out_dir, known_tools: set[str] | None = None) -> Path | None`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_synth_validation.py`：

```python
"""G6 接入:合成/改进的产物必须过工具名校验,编错工具名的候选不写盘。"""

from app.agent.skills.synthesizer import (
    build_tool_hint,
    improve_skill,
    synthesize_one,
    synthesize_skills,
)
from tests.test_skill_synth import FakeClient, REFUND_SAMPLES, REFUND_SKILL_MD

GOOD_TOOLS_MD = """---
name: refund-fast-track
description: 当用户想退款/退货时使用的快速处理流程。
---
第一步：调用 `query_order` 确认订单。
第二步：调用 `apply_refund` 提交退款。
"""

BAD_TOOLS_MD = """---
name: refund-fast-track
description: 当用户想退款/退货时使用的快速处理流程。
---
第一步：调用 `order_list` 拉订单。
"""


def test_build_tool_hint_lists_real_tools():
    hint = build_tool_hint()
    assert "list_user_orders" in hint
    assert "query_coupons" in hint
    assert "只能使用" in hint


def test_synthesize_one_injects_tool_hint_into_prompt():
    client = FakeClient([GOOD_TOOLS_MD])
    synthesize_one(client, "test-model", REFUND_SAMPLES)
    system_msg = client.calls[0]["messages"][0]["content"]
    assert "list_user_orders" in system_msg   # 真实工具清单已注入 system prompt


def test_synthesize_one_rejects_unknown_tool_output():
    client = FakeClient([BAD_TOOLS_MD])
    assert synthesize_one(client, "test-model", REFUND_SAMPLES) is None


def test_synthesize_skills_skips_unknown_tool_candidate(tmp_path):
    client = FakeClient([BAD_TOOLS_MD])
    out = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(tmp_path))
    assert out == []
    assert not (tmp_path / "refund-fast-track").exists()


def test_synthesize_skills_writes_valid_candidate(tmp_path):
    client = FakeClient([GOOD_TOOLS_MD])
    out = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(tmp_path))
    assert len(out) == 1
    assert out[0].read_text(encoding="utf-8") == GOOD_TOOLS_MD


def test_improve_skill_rejects_unknown_tool_output(tmp_path):
    client = FakeClient([BAD_TOOLS_MD])
    skill = {"name": "refund-fast-track", "content": REFUND_SKILL_MD}
    cases = [{"messages": [{"role": "user", "content": "退款没人管"}], "summary": ""}]
    assert improve_skill(client, "test-model", skill, cases, str(tmp_path)) is None


def test_known_tools_can_be_injected_for_isolation(tmp_path):
    """注入自定义工具集时按注入值判定(便于单测/未来多套工具集)。"""
    client = FakeClient([BAD_TOOLS_MD])
    out = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(tmp_path),
                            known_tools={"order_list"})
    assert len(out) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_synth_validation.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_tool_hint'`

- [ ] **Step 3: 改造 synthesizer.py**

在 `app/agent/skills/synthesizer.py` 顶部，把：

```python
from app.agent.skills.loader import _parse_frontmatter
```

改为：

```python
from app.agent.skills.loader import _parse_frontmatter
from app.agent.skills.validator import known_tool_names, validate_candidate
```

在 `SYNTH_SYSTEM_PROMPT` 定义之后（`IMPROVE_SYSTEM_PROMPT` 之前）插入：

```python
def build_tool_hint(known: set[str] | None = None) -> str:
    """把真实工具清单拼成 prompt 片段,防 LLM 凭空编工具名(实测编过 order_list)。"""
    names = sorted(known if known is not None else known_tool_names())
    return (
        "\n可用工具清单(**只能使用**下列工具名,禁止编造其它工具):\n"
        + "\n".join(f"- {n}" for n in names)
        + "\n引用工具时用反引号包裹,例如 `query_order`。\n"
    )
```

- [ ] **Step 4: 让 synthesize_one 注入清单并校验产物**

把 `synthesize_one` 整个函数替换为：

```python
def synthesize_one(client, model: str, group: list[dict],
                   system_prompt: str = SYNTH_SYSTEM_PROMPT,
                   known_tools: set[str] | None = None) -> dict | None:
    """LLM 从同类样本归纳出一个候选 skill。

    - system_prompt 可替换(金牌客服蒸馏用不同的归纳指令)。
    - 真实工具清单注入 system prompt;产物再过 validate_candidate 兜底——
      坏 frontmatter 或引用未知工具 → 返回 None,调用方跳过不崩。
    """
    known = known_tools if known_tools is not None else known_tool_names()
    prompt = _build_prompt(group)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt + build_tool_hint(known)},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""

    report = validate_candidate(content, known=known)
    if not report["valid"]:
        return None

    return {"name": report["name"], "content": content}
```

- [ ] **Step 5: 让 synthesize_skills 透传参数**

把 `synthesize_skills` 的签名与内部调用改为：

```python
def synthesize_skills(client, model: str, samples: list[dict], out_dir: str,
                      system_prompt: str = SYNTH_SYSTEM_PROMPT,
                      known_tools: set[str] | None = None) -> list[Path]:
    """聚类 + 逐组合成候选 skill,写入 out_dir/<name>/SKILL.md。

    - 空样本 → [],不写文件。
    - 样本数 <2 的组跳过(单例不成"重复模式")。
    - 坏输出/引用未知工具的组跳过(fail-soft),不影响其他组。
    """
    if not samples:
        return []

    known = known_tools if known_tools is not None else known_tool_names()
    out_root = Path(out_dir)
    written: list[Path] = []

    for _label, group in group_samples(samples).items():
        if len(group) < MIN_GROUP_SIZE:
            continue

        result = synthesize_one(client, model, group,
                               system_prompt=system_prompt, known_tools=known)
        if result is None:
            continue

        skill_dir = out_root / result["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(result["content"], encoding="utf-8")
        written.append(skill_file)

    return written
```

- [ ] **Step 6: 让 improve_skill 也过校验**

在 `improve_skill` 中，把签名与校验段替换。签名改为：

```python
def improve_skill(
    client, model: str, skill: dict, failure_cases: list[dict], out_dir: str,
    known_tools: set[str] | None = None,
) -> Path | None:
```

把函数体里 LLM 调用与校验的这一段：

```python
    prompt = _build_improve_prompt(skill, failure_cases)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": IMPROVE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""

    meta = _parse_frontmatter(content)
    name = str(meta.get("name") or "").strip()
    if not name:
        return None
```

替换为：

```python
    known = known_tools if known_tools is not None else known_tool_names()
    prompt = _build_improve_prompt(skill, failure_cases)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": IMPROVE_SYSTEM_PROMPT + build_tool_hint(known)},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""

    report = validate_candidate(content, known=known)
    if not report["valid"]:
        return None
    name = report["name"]
```

- [ ] **Step 7: 运行新测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_synth_validation.py -v`
Expected: PASS（7 passed）

- [ ] **Step 8: 回归既有合成测试（旧断言可能因校验变严而需调整）**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_synth.py tests/test_synth_loop.py tests/test_user_modeling.py -v`
Expected: PASS。

若 `test_skill_synth.py` 中某条用例因 `REFUND_SKILL_MD`/`LOGISTICS_SKILL_MD` 正文不含任何反引号工具名而失败——它们不会失败（没有工具引用 ⇒ `unknown_tools` 为空 ⇒ 校验通过）。若有用例断言 `client.calls[0]["messages"][0]["content"] == SYNTH_SYSTEM_PROMPT`（完全相等），改为 `.startswith(SYNTH_SYSTEM_PROMPT)`。

- [ ] **Step 9: 提交**

```bash
git add app/agent/skills/synthesizer.py tests/test_skill_synth_validation.py tests/test_skill_synth.py
git commit -m "feat(skill): 合成器注入真实工具清单并强制产物过校验(挡住编造工具名)"
```

---

### Task 5: 按真实轨迹归集失败案例（G3）

**Files:**
- Create: `app/agent/skills/failure_cases.py`
- Test: `tests/test_skill_failure_cases.py`

**Interfaces:**
- Consumes: `Database.list_skill_traces(...)`（Task 1）、`Database.list_recent_archives(...)`（既有）、`OUTCOME_HANDOFF` / `OUTCOME_TOOL_ERROR`（Task 2）
- Produces:
  - `FAILURE_OUTCOMES: list[str]`（`["handoff", "tool_error"]`）
  - `collect_failures_by_skill(traces: list[dict], archives: list[dict], max_per_skill: int = 5) -> dict[str, list[dict]]`
    返回 `{skill_name: [归档会话样本, ...]}`，样本形状与 `session_archive` 一致（含 `messages` list / `summary`），可直接喂 `improve_skill`。

> **替代的是什么**：现有 `app/scripts/synthesize_skills.py::related_failures` 靠「首条消息关键词 ∩ skill description 关键词」猜关联。本任务改用 `skill_traces` 里「这一轮确实加载了该 skill，且结局是转人工/工具失败」的事实关联。旧函数保留不动（其单测继续跑），闭环入口优先用新的。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_failure_cases.py`：

```python
"""G3 失败案例采集:用真实执行轨迹(skill_traces)关联失败会话,替代关键词猜测。"""

from app.agent.skills.failure_cases import FAILURE_OUTCOMES, collect_failures_by_skill


def _trace(session_id, skill_name, outcome):
    return {"session_id": session_id, "skill_name": skill_name,
            "outcome": outcome, "tool_calls": []}


def _archive(session_id, text, summary=""):
    return {"session_id": session_id, "user_id": "u1", "summary": summary,
            "messages": [{"role": "user", "content": text},
                         {"role": "assistant", "content": "处理中"}]}


def test_failure_outcomes_are_handoff_and_tool_error():
    assert set(FAILURE_OUTCOMES) == {"handoff", "tool_error"}


def test_collects_only_failed_traces_grouped_by_skill():
    traces = [
        _trace("s1", "process-return", "handoff"),
        _trace("s2", "process-return", "success"),      # 成功轮不采
        _trace("s3", "track-order", "tool_error"),
    ]
    archives = [_archive("s1", "退款一直没人处理"),
                _archive("s2", "怎么退货"),
                _archive("s3", "快递到哪了")]

    grouped = collect_failures_by_skill(traces, archives)

    assert set(grouped) == {"process-return", "track-order"}
    assert [m["content"] for m in grouped["process-return"][0]["messages"]][0] == "退款一直没人处理"
    assert len(grouped["track-order"]) == 1


def test_trace_without_matching_archive_is_skipped():
    traces = [_trace("missing", "process-return", "handoff")]
    assert collect_failures_by_skill(traces, []) == {}


def test_duplicate_sessions_deduped():
    """同一会话多轮失败只算一个样本,避免同内容重复喂 LLM。"""
    traces = [_trace("s1", "process-return", "handoff"),
              _trace("s1", "process-return", "tool_error")]
    archives = [_archive("s1", "退款没人管")]

    grouped = collect_failures_by_skill(traces, archives)
    assert len(grouped["process-return"]) == 1


def test_respects_max_per_skill():
    traces = [_trace(f"s{i}", "process-return", "handoff") for i in range(5)]
    archives = [_archive(f"s{i}", f"问题{i}") for i in range(5)]

    grouped = collect_failures_by_skill(traces, archives, max_per_skill=2)
    assert len(grouped["process-return"]) == 2


def test_empty_inputs_return_empty():
    assert collect_failures_by_skill([], []) == {}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_failure_cases.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.failure_cases'`

- [ ] **Step 3: 实现 failure_cases.py**

创建 `app/agent/skills/failure_cases.py`：

```python
"""G3 失败案例采集:按真实执行轨迹把失败会话归到具体 skill 名下。

与旧做法的区别:`app/scripts/synthesize_skills.py::related_failures` 靠"首条用户
消息关键词 ∩ skill description 关键词"猜关联;本模块直接读 `skill_traces`——
"这一轮确实加载了该 skill,且结局是转人工/工具失败"是事实,不是猜测。

产出的样本形状与 `session_archive` 一致(messages list / summary),可直接喂
`synthesizer.improve_skill`。
"""

from __future__ import annotations

from app.agent.skills.execution_trace import OUTCOME_HANDOFF, OUTCOME_TOOL_ERROR

# 视为"该 skill 没搞定"的结局:转人工 / 工具报错
FAILURE_OUTCOMES = [OUTCOME_HANDOFF, OUTCOME_TOOL_ERROR]


def collect_failures_by_skill(
    traces: list[dict], archives: list[dict], max_per_skill: int = 5,
) -> dict[str, list[dict]]:
    """把失败轨迹关联到归档会话,按 skill 分组返回可用于改进的样本。

    - 只取 outcome 命中 FAILURE_OUTCOMES 的轨迹;
    - 按 session_id 关联归档会话,关联不到的轨迹跳过(会话尚未归档);
    - 同一会话在同一 skill 下只算一个样本(去重,避免重复内容喂 LLM);
    - 每个 skill 最多 max_per_skill 条(控 prompt 体积与成本)。
    """
    by_session = {a.get("session_id"): a for a in archives if a.get("session_id")}

    grouped: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()

    for trace in traces:
        if trace.get("outcome") not in FAILURE_OUTCOMES:
            continue
        skill_name = str(trace.get("skill_name") or "").strip()
        session_id = trace.get("session_id")
        if not skill_name or not session_id:
            continue

        archive = by_session.get(session_id)
        if archive is None:
            continue

        key = (skill_name, session_id)
        if key in seen:
            continue

        bucket = grouped.setdefault(skill_name, [])
        if len(bucket) >= max_per_skill:
            continue

        seen.add(key)
        bucket.append(archive)

    return grouped
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_failure_cases.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add app/agent/skills/failure_cases.py tests/test_skill_failure_cases.py
git commit -m "feat(skill): 按真实执行轨迹归集失败案例(替代关键词猜测)"
```

---

### Task 6: 金牌客服语料蒸馏（G5）

**Files:**
- Create: `app/agent/skills/golden_corpus.py`
- Test: `tests/test_golden_corpus.py`

**Interfaces:**
- Consumes: `synthesize_skills(..., system_prompt=..., known_tools=...)`（Task 4）
- Produces:
  - `HUMAN_AGENT_INTENT: str`（`"human_agent"`）
  - `is_human_handled(archived: dict) -> bool`
  - `extract_golden_samples(archives: list[dict]) -> list[dict]`
  - `GOLDEN_SYSTEM_PROMPT: str`
  - `synthesize_from_golden(client, model, archives, out_dir, known_tools=None) -> list[Path]`

> **数据来源事实**：坐席在工作台的人工回复由 `app/api/app.py::admin_session_reply` 写成 assistant 消息，内容是 JSON 且 `intent` 固定为 `"human_agent"`。这是精确标记，无需启发式猜测——这正是「从金牌客服对话蒸馏」的现成语料。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_golden_corpus.py`：

```python
"""G5 金牌客服语料:人工接管(intent=human_agent)的会话才是"人救过场"的优质样本。"""

import json

from app.agent.skills.golden_corpus import (
    GOLDEN_SYSTEM_PROMPT,
    extract_golden_samples,
    is_human_handled,
    synthesize_from_golden,
)
from tests.test_skill_synth import FakeClient

GOLDEN_MD = """---
name: refund-human-playbook
description: 用户退款受阻升级处理时的人工经验流程。
---
第一步：调用 `query_order` 核对订单。
第二步：调用 `apply_refund` 提交并同步进度。
"""


def _human_msg(text):
    return {"role": "assistant", "content": json.dumps(
        {"intent": "human_agent", "confidence": 1.0, "reply": text,
         "requires_human": False, "follow_up_question": None}, ensure_ascii=False)}


def _ai_msg(text):
    return {"role": "assistant", "content": json.dumps(
        {"intent": "return_request", "confidence": 0.9, "reply": text,
         "requires_human": False, "follow_up_question": None}, ensure_ascii=False)}


def _archive(session_id, msgs, summary=""):
    return {"session_id": session_id, "user_id": "u1", "summary": summary, "messages": msgs}


def test_is_human_handled_detects_human_agent_intent():
    a = _archive("s1", [{"role": "user", "content": "要退货"}, _human_msg("我已帮您登记")])
    assert is_human_handled(a) is True


def test_pure_ai_session_not_human_handled():
    a = _archive("s2", [{"role": "user", "content": "要退货"}, _ai_msg("请提供订单号")])
    assert is_human_handled(a) is False


def test_plain_text_assistant_content_not_human_handled():
    """assistant 内容不是 JSON(旧格式/纯文本)时不误判成人工。"""
    a = _archive("s3", [{"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "你好"}])
    assert is_human_handled(a) is False


def test_extract_golden_samples_filters_to_human_sessions():
    archives = [
        _archive("s1", [{"role": "user", "content": "退货被拒"}, _human_msg("我特批给您")]),
        _archive("s2", [{"role": "user", "content": "查订单"}, _ai_msg("已查到")]),
    ]
    samples = extract_golden_samples(archives)
    assert [s["session_id"] for s in samples] == ["s1"]


def test_golden_prompt_mentions_human_expert():
    assert "人工" in GOLDEN_SYSTEM_PROMPT


def test_synthesize_from_golden_writes_candidate(tmp_path):
    archives = [
        _archive("s1", [{"role": "user", "content": "退款一直不到账"}, _human_msg("我帮您加急")]),
        _archive("s2", [{"role": "user", "content": "退货运费谁承担"}, _human_msg("这单我们承担")]),
    ]
    client = FakeClient([GOLDEN_MD])
    out = synthesize_from_golden(client, "test-model", archives, str(tmp_path))

    assert len(out) == 1
    assert out[0].read_text(encoding="utf-8") == GOLDEN_MD
    system_msg = client.calls[0]["messages"][0]["content"]
    assert "人工" in system_msg


def test_synthesize_from_golden_no_human_sessions_no_llm_call(tmp_path):
    archives = [_archive("s2", [{"role": "user", "content": "查订单"}, _ai_msg("已查到")])]
    client = FakeClient([])
    out = synthesize_from_golden(client, "test-model", archives, str(tmp_path))

    assert out == []
    assert client.calls == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_golden_corpus.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.golden_corpus'`

- [ ] **Step 3: 实现 golden_corpus.py**

创建 `app/agent/skills/golden_corpus.py`：

```python
"""G5 金牌客服语料蒸馏:从人工接管过的会话里学人的处理经验。

语料来源是确定的、不需要启发式猜测:坐席在工作台的人工回复由
`app/api/app.py::admin_session_reply` 写成 assistant 消息,内容是 JSON 且
`intent` 固定为 "human_agent"。凡含这种消息的归档会话,就是"AI 没搞定、人救了
场"的优质样本——正是最该被沉淀成 skill 的部分。

复用 `synthesizer.synthesize_skills`,只替换 system prompt(归纳视角不同:
这里要学人工的判断与话术,而不是总结 AI 自己的套路)。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.skills.synthesizer import synthesize_skills

HUMAN_AGENT_INTENT = "human_agent"

GOLDEN_SYSTEM_PROMPT = """你是电商客服 Skill 蒸馏器。

下面会给你一组**人工客服接管处理过**的历史会话样本(AI 未能独立解决,由人工
坐席接手并解决)。请从人工的处理方式中归纳出一份可复用的 SKILL.md,让 AI 下次
遇到同类问题时能像这位人工客服一样处理。

归纳重点:
- 人工是怎么判断的(先确认什么、依据什么下结论);
- 人工做了哪些 AI 漏掉的步骤(补充核对、主动让利、升级处理);
- 人工的话术分寸(如何安抚、如何给承诺而不越权)。

严格要求:
- 只输出一份完整的 SKILL.md 文本,不要任何额外说明、不要用 markdown 代码块包裹。
- 必须以如下格式开头(frontmatter):
---
name: <kebab-case 技能名>
description: <一句话描述适用场景与关键词,供路由匹配>
---
- frontmatter 之后是 Markdown body,写出步骤化流程。
"""


def is_human_handled(archived: dict) -> bool:
    """该归档会话是否被人工接管过(存在 intent=human_agent 的 assistant 消息)。

    assistant 内容非 JSON(旧格式/纯文本)时按"非人工"处理,不误判。
    """
    for msg in archived.get("messages") or []:
        if msg.get("role") != "assistant":
            continue
        try:
            data = json.loads(str(msg.get("content") or ""))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("intent") == HUMAN_AGENT_INTENT:
            return True
    return False


def extract_golden_samples(archives: list[dict]) -> list[dict]:
    """筛出被人工接管过的归档会话(金牌语料)。"""
    return [a for a in archives if is_human_handled(a)]


def synthesize_from_golden(client, model: str, archives: list[dict], out_dir: str,
                           known_tools: set[str] | None = None) -> list[Path]:
    """从金牌客服语料蒸馏候选 skill;无人工会话则不调 LLM、直接返回 []。"""
    samples = extract_golden_samples(archives)
    if not samples:
        return []
    return synthesize_skills(client, model, samples, out_dir,
                             system_prompt=GOLDEN_SYSTEM_PROMPT, known_tools=known_tools)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_golden_corpus.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add app/agent/skills/golden_corpus.py tests/test_golden_corpus.py
git commit -m "feat(skill): 从人工接管语料蒸馏候选 skill(金牌客服经验)"
```

---

### Task 7: 评测用例按 skill 打标与筛选（G4 前置）

**Files:**
- Modify: `app/evaluation/dataset.py:20-45`
- Modify: `app/evaluation/cases.json`（3 条用例）
- Test: `tests/test_eval_skill_filter.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `EvalCase.related_skills: list[str]`（默认空列表，向后兼容旧 cases.json）
  - `filter_by_skill(cases: list[EvalCase], skill_name: str) -> list[EvalCase]`

> **为什么需要**：门禁若跑全量数据集要两遍全量 LLM 评测，慢且贵。按 skill 打标后只跑相关子集，让门禁在实际成本下可用。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_eval_skill_filter.py`：

```python
"""G4 前置:评测用例按 skill 打标,门禁只跑与候选相关的子集(控成本)。"""

import json

from app.evaluation.dataset import EvalCase, filter_by_skill, load_dataset


def test_eval_case_defaults_related_skills_empty():
    case = EvalCase(id="x", description="d", turns=["hi"])
    assert case.related_skills == []


def test_filter_by_skill_selects_tagged_cases():
    cases = [
        EvalCase(id="a", description="", turns=["1"], related_skills=["process-return"]),
        EvalCase(id="b", description="", turns=["2"], related_skills=["track-order"]),
        EvalCase(id="c", description="", turns=["3"]),
    ]
    assert [c.id for c in filter_by_skill(cases, "process-return")] == ["a"]
    assert filter_by_skill(cases, "unknown-skill") == []


def test_load_dataset_reads_related_skills(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [
        {"id": "a", "description": "d", "turns": ["hi"], "related_skills": ["process-return"]},
        {"id": "b", "description": "d", "turns": ["hi"]},
    ]}, ensure_ascii=False), encoding="utf-8")

    cases = load_dataset(path)
    assert cases[0].related_skills == ["process-return"]
    assert cases[1].related_skills == []


def test_real_dataset_has_skill_tagged_cases():
    """正式数据集必须有打标用例,否则门禁永远 fail-closed 拒绝一切候选。"""
    cases = load_dataset("app/evaluation/cases.json")
    tagged = [c for c in cases if c.related_skills]
    assert tagged, "cases.json 中至少要有一条 related_skills 打标用例"
    assert filter_by_skill(cases, "process-return")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_eval_skill_filter.py -v`
Expected: FAIL — `ImportError: cannot import name 'filter_by_skill'`

- [ ] **Step 3: 加字段与筛选函数**

在 `app/evaluation/dataset.py` 的 `EvalCase` 中，把：

```python
    expected_route: str | None = None  # 多 Agent 期望路由（presale/postsale/complaint），可选
```

改为：

```python
    expected_route: str | None = None  # 多 Agent 期望路由（presale/postsale/complaint），可选

    # ---------- G4 门禁 ----------
    related_skills: list[str] = field(default_factory=list)  # 本用例覆盖哪些 skill，供候选灰度评测筛子集
```

在文件末尾 `load_dataset` 之后追加：

```python
def filter_by_skill(cases: list[EvalCase], skill_name: str) -> list[EvalCase]:
    """筛出覆盖指定 skill 的用例(G4 门禁只跑相关子集,避免两遍全量评测)。"""
    return [c for c in cases if skill_name in (c.related_skills or [])]
```

- [ ] **Step 4: 给 3 条用例打标**

在 `app/evaluation/cases.json` 中做三处编辑。

其一，`logistics_track` 用例，把：

```json
      "expected_tools": ["query_logistics"],
      "min_tool_calls": 1,
      "max_tokens": 6000
    },
    {
      "id": "logistics_no_tracking",
```

改为：

```json
      "expected_tools": ["query_logistics"],
      "min_tool_calls": 1,
      "max_tokens": 6000,
      "related_skills": ["track-order"]
    },
    {
      "id": "logistics_no_tracking",
```

其二，`product_out_of_stock` 用例，把：

```json
      "expected_tools": ["query_product"],
      "min_tool_calls": 1,
      "max_tokens": 6000
    },
    {
      "id": "return_request",
```

改为：

```json
      "expected_tools": ["query_product"],
      "min_tool_calls": 1,
      "max_tokens": 6000,
      "related_skills": ["product-recommend"]
    },
    {
      "id": "return_request",
```

其三，`return_request` 用例，把：

```json
      "expected_keywords": ["退款"],
      "expected_requires_human": false,
      "expected_tools": [],
      "max_tokens": 8000
    },
```

改为：

```json
      "expected_keywords": ["退款"],
      "expected_requires_human": false,
      "expected_tools": [],
      "max_tokens": 8000,
      "related_skills": ["process-return"]
    },
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_eval_skill_filter.py -v`
Expected: PASS（4 passed）

- [ ] **Step 6: 回归评测相关测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_evaluation.py tests/test_eval_runner.py tests/test_eval_api.py -v`
Expected: PASS（全绿）

- [ ] **Step 7: 提交**

```bash
git add app/evaluation/dataset.py app/evaluation/cases.json tests/test_eval_skill_filter.py
git commit -m "feat(eval): 评测用例支持 related_skills 打标与按 skill 筛选(门禁前置)"
```

---

### Task 8: 候选灰度评测门禁（G4）

**Files:**
- Create: `app/agent/skills/gate.py`
- Test: `tests/test_skill_gate.py`

**Interfaces:**
- Consumes: `app.evaluation.regression.compare_to_baseline(current, baseline, tolerance)`（既有）、`app.evaluation.dataset.filter_by_skill`（Task 7）
- Produces:
  - `build_shadow_dir(definitions_dir: str, skill_name: str, candidate_path: str, dest_root: str) -> Path`
  - `gate_candidate(skill_name: str, candidate_path: str, definitions_dir: str, dest_root: str, eval_fn, case_ids: list[str], tolerance: float = 0.05) -> dict`
    返回 `{"promote": bool, "reason": str, "baseline": dict | None, "candidate": dict | None, "comparison": dict | None, "shadow_dir": str | None}`
  - `default_eval_fn(skills_dir: str, case_ids: list[str]) -> dict`（真实评测实现，跑 LLM；单测不用它）
  - `gate_case_ids(skill_name: str, dataset_path: str) -> list[str]`

> **fail-closed**：`case_ids` 为空 ⇒ 无法证明候选不劣化 ⇒ `promote=False`, `reason="no_gate_cases"`。这就是「劣化回滚」的前置：不能证明更好，就不许上。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_gate.py`：

```python
"""G4 门禁:候选先在影子目录里跑评测,劣化即拒绝转正(fail-closed)。

注入 fake eval_fn,不触网、不跑真 LLM。
"""

from pathlib import Path

from app.agent.skills.gate import build_shadow_dir, gate_candidate

LIVE_MD = """---
name: process-return
description: 退货处理流程(现行版)。
---
现行正文。
"""

OTHER_MD = """---
name: track-order
description: 订单物流跟踪。
---
其它 skill 正文。
"""

CANDIDATE_MD = """---
name: process-return
description: 退货处理流程(候选改进版)。
---
候选正文。
"""


def _setup(tmp_path):
    definitions = tmp_path / "definitions"
    (definitions / "process-return").mkdir(parents=True)
    (definitions / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")
    (definitions / "track-order").mkdir(parents=True)
    (definitions / "track-order" / "SKILL.md").write_text(OTHER_MD, encoding="utf-8")
    # 候选目录嵌在 definitions 下,验证不会被复制进影子目录
    (definitions / "_candidates" / "process-return").mkdir(parents=True)
    (definitions / "_candidates" / "process-return" / "SKILL.md").write_text(
        CANDIDATE_MD, encoding="utf-8")
    return definitions


def test_build_shadow_dir_substitutes_candidate_keeps_others(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"

    shadow = build_shadow_dir(str(definitions), "process-return", str(candidate),
                              str(tmp_path / "shadow"))

    assert (shadow / "process-return" / "SKILL.md").read_text(encoding="utf-8") == CANDIDATE_MD
    assert (shadow / "track-order" / "SKILL.md").read_text(encoding="utf-8") == OTHER_MD
    assert not (shadow / "_candidates").exists()   # 候选目录不进影子目录


def test_build_shadow_dir_supports_brand_new_skill(tmp_path):
    definitions = _setup(tmp_path)
    new_candidate = tmp_path / "cand" / "coupon-lookup" / "SKILL.md"
    new_candidate.parent.mkdir(parents=True)
    new_candidate.write_text(
        "---\nname: coupon-lookup\ndescription: 查券。\n---\n正文。", encoding="utf-8")

    shadow = build_shadow_dir(str(definitions), "coupon-lookup", str(new_candidate),
                              str(tmp_path / "shadow2"))

    assert (shadow / "coupon-lookup" / "SKILL.md").exists()
    assert (shadow / "process-return" / "SKILL.md").exists()


def _fake_eval(scores: dict):
    """按 skills_dir 是否为影子目录返回不同 pass_rate。"""
    calls = []

    def eval_fn(skills_dir: str, case_ids: list[str]) -> dict:
        calls.append((skills_dir, list(case_ids)))
        key = "shadow" if "shadow" in Path(skills_dir).name else "live"
        return {"summary": {"pass_rate": scores[key], "avg_process_score": None,
                            "avg_result_score": None}}

    return eval_fn, calls


def test_gate_promotes_when_candidate_better(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, calls = _fake_eval({"live": 0.60, "shadow": 0.80})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, ["return_request"])

    assert result["promote"] is True
    assert result["baseline"]["pass_rate"] == 0.60
    assert result["candidate"]["pass_rate"] == 0.80
    assert len(calls) == 2
    assert calls[0][1] == ["return_request"]


def test_gate_rejects_when_candidate_regresses(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, _ = _fake_eval({"live": 0.80, "shadow": 0.50})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, ["return_request"],
                            tolerance=0.05)

    assert result["promote"] is False
    assert result["comparison"]["regressed"] is True
    assert "劣化" in result["reason"]


def test_gate_tolerates_small_dip_within_tolerance(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, _ = _fake_eval({"live": 0.80, "shadow": 0.78})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, ["return_request"],
                            tolerance=0.05)

    assert result["promote"] is True   # 掉 0.02 < 容差 0.05


def test_gate_fail_closed_without_cases(tmp_path):
    """没有评测用例 ⇒ 无法证明不劣化 ⇒ 拒绝转正,且不调 eval。"""
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, calls = _fake_eval({"live": 1.0, "shadow": 1.0})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, [])

    assert result["promote"] is False
    assert result["reason"] == "no_gate_cases"
    assert calls == []


def test_gate_fail_closed_when_eval_raises(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"

    def boom(skills_dir, case_ids):
        raise RuntimeError("评测炸了")

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), boom, ["return_request"])

    assert result["promote"] is False
    assert "评测炸了" in result["reason"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.gate'`

- [ ] **Step 3: 实现 gate.py**

创建 `app/agent/skills/gate.py`：

```python
"""G4 候选灰度评测门禁:候选先在"影子技能目录"里跑一遍评测,和现行版本比,
劣化超过容差就拒绝转正。

这是自进化敢落地的前提——多客的说法是"新旧 Skill 灰度对比成功率,劣化版本回滚"。
本模块只负责判定(promote true/false),真正的写入/备份/回滚在
`app/scripts/promote_skill.py`。

fail-closed 原则:没有相关评测用例、或评测本身抛异常,都返回 promote=False。
"不能证明更好"就不许上,绝不默认放行。

影子目录:把 definitions/ 下的正式 skill 全量复制一份(跳过 _ 前缀的辅助目录),
再用候选覆盖/新增目标 skill。这样评测跑的是"只换了这一个 skill"的完整技能集。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.evaluation.regression import compare_to_baseline

# definitions/ 下这些前缀的目录是辅助目录(候选/备份),不属于正式技能集
_AUX_PREFIX = "_"


def build_shadow_dir(definitions_dir: str, skill_name: str,
                     candidate_path: str, dest_root: str) -> Path:
    """构建影子技能目录:正式技能全量复制 + 用候选覆盖(或新增)目标 skill。

    dest_root 若已存在会被清空重建,保证每次门禁跑在干净目录上。
    """
    src = Path(definitions_dir)
    dest = Path(dest_root)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for child in sorted(src.iterdir()):
        if not child.is_dir() or child.name.startswith(_AUX_PREFIX):
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.exists():
            continue
        target = dest / child.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_file, target / "SKILL.md")

    # 候选覆盖/新增目标 skill(新建 skill 时正式目录里还没有它)
    target = dest / skill_name
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(candidate_path), target / "SKILL.md")
    return dest


def gate_candidate(skill_name: str, candidate_path: str, definitions_dir: str,
                   dest_root: str, eval_fn, case_ids: list[str],
                   tolerance: float = 0.05) -> dict:
    """灰度对比候选与现行版本,判定是否允许转正。

    - `eval_fn(skills_dir: str, case_ids: list[str]) -> report`,report 含 "summary";
      注入式设计,单测传 fake、生产传 default_eval_fn。
    - 无 case_ids → promote=False, reason="no_gate_cases"(fail-closed);
    - eval_fn 抛异常 → promote=False,reason 带异常信息(fail-closed);
    - 复用 `compare_to_baseline`:候选相对现行掉点超过 tolerance 即判劣化。
    """
    if not case_ids:
        return {"promote": False, "reason": "no_gate_cases", "baseline": None,
                "candidate": None, "comparison": None, "shadow_dir": None}

    try:
        shadow = build_shadow_dir(definitions_dir, skill_name, candidate_path, dest_root)
        baseline = (eval_fn(definitions_dir, case_ids) or {}).get("summary") or {}
        candidate = (eval_fn(str(shadow), case_ids) or {}).get("summary") or {}
    except Exception as exc:  # noqa: BLE001 门禁 fail-closed:评测失败=不许上
        return {"promote": False, "reason": f"评测执行失败: {exc}", "baseline": None,
                "candidate": None, "comparison": None, "shadow_dir": None}

    comparison = compare_to_baseline(candidate, baseline, tolerance)
    regressed = comparison["regressed"]
    reason = "候选劣化超过容差,拒绝转正" if regressed else "候选未劣化,允许转正"
    return {"promote": not regressed, "reason": reason, "baseline": baseline,
            "candidate": candidate, "comparison": comparison, "shadow_dir": str(shadow)}


def gate_case_ids(skill_name: str, dataset_path: str) -> list[str]:
    """取与该 skill 相关的评测用例 id(供 CLI 组装门禁入参)。"""
    from app.evaluation.dataset import filter_by_skill, load_dataset

    return [c.id for c in filter_by_skill(load_dataset(dataset_path), skill_name)]


def default_eval_fn(skills_dir: str, case_ids: list[str]) -> dict:
    """真实评测实现:临时把 settings.skills_dir 指到给定目录,只跑指定用例。

    会真调 LLM(评测本身要跑 Agent),故单测不用本函数。settings 改动在
    finally 中还原,避免污染同进程后续调用。
    """
    from openai import OpenAI

    from app.config.settings import settings
    from app.evaluation.dataset import load_dataset
    from app.evaluation.evaluator import Evaluator
    from app.evaluation.sandbox import Sandbox

    original_dir = settings.skills_dir
    settings.skills_dir = skills_dir
    try:
        cases = [c for c in load_dataset(settings.eval_dataset_path) if c.id in set(case_ids)]
        client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        evaluator = Evaluator(
            sandbox=Sandbox(mode="single"), client=client, model=settings.model_name,
            use_judge=settings.eval_use_judge, pass_threshold=settings.eval_pass_threshold,
        )
        return evaluator.run_all(cases)
    finally:
        settings.skills_dir = original_dir
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_gate.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add app/agent/skills/gate.py tests/test_skill_gate.py
git commit -m "feat(skill): 候选灰度评测门禁(影子目录对比,劣化即拒,fail-closed)"
```

---

### Task 9: 转正与回滚 CLI（G4 落地）

**Files:**
- Create: `app/scripts/promote_skill.py`
- Test: `tests/test_promote_skill.py`

**Interfaces:**
- Consumes: `validate_candidate`（Task 3）、`gate_candidate` / `gate_case_ids` / `default_eval_fn`（Task 8）
- Produces:
  - `list_candidates(candidates_dir: str, definitions_dir: str) -> list[dict]`（每项 `{"name", "path", "valid", "unknown_tools", "errors", "is_improvement"}`，`is_improvement` 表示正式目录已存在同名 skill）
  - 模块常量 `DEFINITIONS_DIR` / `CANDIDATES_DIR` / `ARCHIVE_DIR`（供 Task 10、Task 11 复用，不要各自硬编码路径）
  - `backup_current(definitions_dir: str, skill_name: str, archive_dir: str, timestamp: str) -> Path | None`
  - `promote(skill_name: str, definitions_dir: str, candidates_dir: str, archive_dir: str, gate_result: dict | None, force: bool, timestamp: str) -> dict`
  - `rollback(skill_name: str, definitions_dir: str, archive_dir: str) -> dict`

> **分级授权铁律的唯一写入点**：本文件是全仓库唯一允许写 `definitions/` 正式目录的代码。校验不过一律拒绝（`--force` 也拒绝）；门禁不过则只有显式 `--force` 才放行。转正前把现行版本备份到 `_archive/<name>/<timestamp>/SKILL.md`，`rollback` 从最新备份恢复——这就是「劣化版本回滚」。
>
> Task 14 的看门狗会在灰度胜出后以 `force=True` 调用本文件的 `promote`（实战数据强于离线门禁）；但高风险档由看门狗自己拦在调用之前，**永远不会走到这里**。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_promote_skill.py`：

```python
"""G4 落地:转正 CLI(校验→门禁→备份→写正式目录)与回滚。

强调:这是全仓库唯一允许写 definitions/ 正式目录的自动化路径。
"""

from app.scripts.promote_skill import (
    backup_current,
    list_candidates,
    promote,
    rollback,
)

LIVE_MD = """---
name: process-return
description: 退货处理流程(现行版)。
---
现行正文。
"""

CANDIDATE_MD = """---
name: process-return
description: 退货处理流程(候选改进版)。
---
第一步：调用 `query_order` 核对订单。
"""

BAD_CANDIDATE_MD = """---
name: coupon-lookup
description: 查券流程。
---
第一步：调用 `coupon_query` 查券。
"""


def _dirs(tmp_path, with_live=True, candidates=None):
    definitions = tmp_path / "definitions"
    definitions.mkdir(parents=True, exist_ok=True)
    if with_live:
        (definitions / "process-return").mkdir(parents=True, exist_ok=True)
        (definitions / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")

    cand_dir = definitions / "_candidates"
    for name, content in (candidates or {}).items():
        (cand_dir / name).mkdir(parents=True, exist_ok=True)
        (cand_dir / name / "SKILL.md").write_text(content, encoding="utf-8")

    archive = definitions / "_archive"
    return str(definitions), str(cand_dir), str(archive)


PASS_GATE = {"promote": True, "reason": "候选未劣化,允许转正"}
FAIL_GATE = {"promote": False, "reason": "候选劣化超过容差,拒绝转正"}


# ---------- list_candidates ----------

def test_list_candidates_reports_validation_and_kind(tmp_path):
    definitions, cand_dir, _ = _dirs(tmp_path, candidates={
        "process-return": CANDIDATE_MD, "coupon-lookup": BAD_CANDIDATE_MD})

    items = {c["name"]: c for c in list_candidates(cand_dir, definitions)}

    assert items["process-return"]["valid"] is True
    assert items["process-return"]["is_improvement"] is True     # 正式目录已有同名
    assert items["coupon-lookup"]["valid"] is False
    assert items["coupon-lookup"]["unknown_tools"] == ["coupon_query"]
    assert items["coupon-lookup"]["is_improvement"] is False     # 全新 skill


def test_list_candidates_empty_dir(tmp_path):
    definitions, cand_dir, _ = _dirs(tmp_path)
    assert list_candidates(cand_dir, definitions) == []


# ---------- backup ----------

def test_backup_current_copies_live_version(tmp_path):
    definitions, _, archive = _dirs(tmp_path)
    path = backup_current(definitions, "process-return", archive, "20260803-120000")

    assert path is not None
    assert path.read_text(encoding="utf-8") == LIVE_MD
    assert path.parts[-2] == "20260803-120000"


def test_backup_current_returns_none_for_new_skill(tmp_path):
    definitions, _, archive = _dirs(tmp_path, with_live=False)
    assert backup_current(definitions, "coupon-lookup", archive, "20260803-120000") is None


# ---------- promote ----------

def test_promote_writes_definitions_and_backs_up(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})

    result = promote("process-return", definitions, cand_dir, archive,
                     gate_result=PASS_GATE, force=False, timestamp="20260803-120000")

    assert result["promoted"] is True
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == CANDIDATE_MD          # 已生效
    backup = tmp_path / "definitions" / "_archive" / "process-return" / "20260803-120000" / "SKILL.md"
    assert backup.read_text(encoding="utf-8") == LIVE_MD             # 旧版已备份


def test_promote_blocked_by_validation(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, with_live=False,
                                           candidates={"coupon-lookup": BAD_CANDIDATE_MD})

    result = promote("coupon-lookup", definitions, cand_dir, archive,
                     gate_result=PASS_GATE, force=False, timestamp="t1")

    assert result["promoted"] is False
    assert "coupon_query" in result["reason"]
    assert not (tmp_path / "definitions" / "coupon-lookup").exists()   # 未写正式目录


def test_promote_blocked_by_gate(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})

    result = promote("process-return", definitions, cand_dir, archive,
                     gate_result=FAIL_GATE, force=False, timestamp="t1")

    assert result["promoted"] is False
    assert "劣化" in result["reason"]
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD   # 正式目录未被改动


def test_promote_force_overrides_gate_but_not_validation(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={
        "process-return": CANDIDATE_MD, "coupon-lookup": BAD_CANDIDATE_MD})

    forced = promote("process-return", definitions, cand_dir, archive,
                     gate_result=FAIL_GATE, force=True, timestamp="t1")
    assert forced["promoted"] is True

    still_blocked = promote("coupon-lookup", definitions, cand_dir, archive,
                            gate_result=FAIL_GATE, force=True, timestamp="t2")
    assert still_blocked["promoted"] is False   # --force 不能绕过工具名校验


def test_promote_missing_candidate(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path)
    result = promote("nope", definitions, cand_dir, archive,
                     gate_result=PASS_GATE, force=False, timestamp="t1")
    assert result["promoted"] is False
    assert "候选不存在" in result["reason"]


# ---------- rollback ----------

def test_rollback_restores_latest_backup(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    promote("process-return", definitions, cand_dir, archive,
            gate_result=PASS_GATE, force=False, timestamp="20260803-120000")

    result = rollback("process-return", definitions, archive)

    assert result["rolled_back"] is True
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD   # 回到现行版


def test_rollback_picks_newest_of_multiple_backups(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    promote("process-return", definitions, cand_dir, archive, PASS_GATE, False, "20260801-090000")
    # 第二次转正:此时现行版已是 CANDIDATE_MD,备份进 20260803
    promote("process-return", definitions, cand_dir, archive, PASS_GATE, False, "20260803-120000")

    rollback("process-return", definitions, archive)
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == CANDIDATE_MD   # 恢复的是最新备份


def test_rollback_without_backup_fails_safely(tmp_path):
    definitions, _, archive = _dirs(tmp_path)
    result = rollback("process-return", definitions, archive)
    assert result["rolled_back"] is False
    assert "无备份" in result["reason"]


def test_archive_dir_not_loaded_by_skill_manager(tmp_path):
    """铁律回归:_archive 嵌套层级保证 SkillManager 不会把备份当成活的 skill。"""
    from app.agent.skills.loader import SkillManager

    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    promote("process-return", definitions, cand_dir, archive, PASS_GATE, False, "t1")

    sm = SkillManager(skills_dir=definitions, enabled=True)
    assert sm.skill_names == ["process-return"]
    assert sm.skill_count == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_promote_skill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.scripts.promote_skill'`

- [ ] **Step 3: 实现 promote_skill.py**

创建 `app/scripts/promote_skill.py`：

```python
"""候选 Skill 转正 / 回滚 CLI(分级授权铁律的唯一写入点)。

本文件是全仓库**唯一**允许写 `definitions/` 正式目录的代码。写入前必须:
  ① 过静态校验(frontmatter + 工具名真实性,validator);
  ② 过灰度评测门禁(影子目录对比,gate);
  ③ 把现行版本备份到 `_archive/<name>/<时间戳>/SKILL.md`。
`--force` 只能跳过②(门禁),**不能**跳过①(校验)——编错工具名的候选永远不许上。

用法:
  python -m app.scripts.promote_skill --list                    列出候选与校验结果
  python -m app.scripts.promote_skill <skill-name>              校验+门禁+转正
  python -m app.scripts.promote_skill <skill-name> --force      跳过门禁(仍校验)
  python -m app.scripts.promote_skill <skill-name> --rollback   从最新备份恢复

备份目录 `_archive/<name>/<ts>/SKILL.md` 比正式 skill 多嵌两层,且 `_archive`
自身不含 SKILL.md,故 SkillManager._discover 不会加载它(与 `_candidates` 同理)。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.skills.validator import validate_candidate  # noqa: E402

DEFINITIONS_DIR = "app/agent/skills/definitions"
CANDIDATES_DIR = "app/agent/skills/definitions/_candidates"
ARCHIVE_DIR = "app/agent/skills/definitions/_archive"


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def list_candidates(candidates_dir: str, definitions_dir: str) -> list[dict]:
    """列出候选及其校验结果。is_improvement=正式目录已存在同名 skill(改进版)。"""
    root = Path(candidates_dir)
    if not root.exists():
        return []

    items: list[dict] = []
    for skill_dir in sorted(root.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_file.exists():
            continue
        report = validate_candidate(skill_file.read_text(encoding="utf-8"))
        items.append({
            "name": skill_dir.name,
            "path": str(skill_file),
            "valid": report["valid"],
            "unknown_tools": report["unknown_tools"],
            "errors": report["errors"],
            "is_improvement": (Path(definitions_dir) / skill_dir.name / "SKILL.md").exists(),
        })
    return items


def backup_current(definitions_dir: str, skill_name: str, archive_dir: str,
                   timestamp: str) -> Path | None:
    """把现行版本备份到 archive_dir/<name>/<timestamp>/SKILL.md。

    正式目录尚无该 skill(全新候选)→ 无需备份,返回 None。
    """
    live = Path(definitions_dir) / skill_name / "SKILL.md"
    if not live.exists():
        return None

    dest_dir = Path(archive_dir) / skill_name / timestamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "SKILL.md"
    shutil.copyfile(live, dest)
    return dest


def promote(skill_name: str, definitions_dir: str, candidates_dir: str, archive_dir: str,
            gate_result: dict | None, force: bool, timestamp: str) -> dict:
    """把候选转正:校验 → 门禁 → 备份 → 写正式目录。

    返回 `{"promoted": bool, "reason": str, "backup": str | None}`。
    任一关卡不过都不写正式目录(force 只放行门禁,不放行校验)。
    """
    candidate = Path(candidates_dir) / skill_name / "SKILL.md"
    if not candidate.exists():
        return {"promoted": False, "reason": f"候选不存在: {candidate}", "backup": None}

    content = candidate.read_text(encoding="utf-8")
    report = validate_candidate(content)
    if not report["valid"]:
        return {"promoted": False,
                "reason": "校验未通过: " + "; ".join(report["errors"]), "backup": None}

    if not force:
        if gate_result is None:
            return {"promoted": False, "reason": "缺少门禁结果,拒绝转正", "backup": None}
        if not gate_result.get("promote"):
            return {"promoted": False,
                    "reason": f"门禁未通过: {gate_result.get('reason', '')}", "backup": None}

    backup = backup_current(definitions_dir, skill_name, archive_dir, timestamp)

    dest_dir = Path(definitions_dir) / skill_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "SKILL.md").write_text(content, encoding="utf-8")

    return {"promoted": True,
            "reason": "已转正" + ("(--force 跳过门禁)" if force else ""),
            "backup": str(backup) if backup else None}


def rollback(skill_name: str, definitions_dir: str, archive_dir: str) -> dict:
    """从最新备份恢复正式目录里的该 skill(劣化回滚)。"""
    skill_archive = Path(archive_dir) / skill_name
    stamps = sorted(
        (d for d in skill_archive.iterdir() if d.is_dir() and (d / "SKILL.md").exists()),
        key=lambda d: d.name,
    ) if skill_archive.exists() else []

    if not stamps:
        return {"rolled_back": False, "reason": f"无备份可回滚: {skill_archive}", "restored_from": None}

    newest = stamps[-1]
    dest_dir = Path(definitions_dir) / skill_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(newest / "SKILL.md", dest_dir / "SKILL.md")
    return {"rolled_back": True, "reason": f"已回滚到 {newest.name}",
            "restored_from": str(newest / "SKILL.md")}


def main() -> None:
    parser = argparse.ArgumentParser(description="候选 Skill 转正 / 回滚")
    parser.add_argument("skill_name", nargs="?", help="要转正/回滚的 skill 名")
    parser.add_argument("--list", action="store_true", help="列出候选与校验结果")
    parser.add_argument("--force", action="store_true", help="跳过评测门禁(仍做校验)")
    parser.add_argument("--rollback", action="store_true", help="从最新备份恢复")
    args = parser.parse_args()

    if args.list:
        items = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
        if not items:
            print("没有候选(先跑 python -m app.scripts.synthesize_skills)")
            return
        for item in items:
            kind = "改进" if item["is_improvement"] else "新建"
            status = "✅ 可转正" if item["valid"] else f"❌ {'; '.join(item['errors'])}"
            print(f"[{kind}] {item['name']}: {status}")
        return

    if not args.skill_name:
        parser.error("需要指定 skill 名,或使用 --list")

    if args.rollback:
        print(rollback(args.skill_name, DEFINITIONS_DIR, ARCHIVE_DIR))
        return

    gate_result = None
    if not args.force:
        from app.agent.skills.gate import default_eval_fn, gate_candidate, gate_case_ids
        from app.config.settings import settings

        case_ids = gate_case_ids(args.skill_name, settings.eval_dataset_path)
        print(f"门禁用例: {case_ids or '(无 → 将拒绝转正,可用 --force 跳过门禁)'}")
        gate_result = gate_candidate(
            skill_name=args.skill_name,
            candidate_path=str(Path(CANDIDATES_DIR) / args.skill_name / "SKILL.md"),
            definitions_dir=DEFINITIONS_DIR,
            dest_root=str(Path(ARCHIVE_DIR).parent / "_shadow"),
            eval_fn=default_eval_fn,
            case_ids=case_ids,
            tolerance=settings.skill_gate_tolerance,
        )
        print(f"门禁结果: promote={gate_result['promote']} | {gate_result['reason']}")
        if gate_result.get("baseline"):
            print(f"  现行 pass_rate={gate_result['baseline'].get('pass_rate')} "
                  f"→ 候选 pass_rate={gate_result['candidate'].get('pass_rate')}")

    result = promote(args.skill_name, DEFINITIONS_DIR, CANDIDATES_DIR, ARCHIVE_DIR,
                     gate_result=gate_result, force=args.force, timestamp=_now_stamp())
    print(result)
    if result["promoted"]:
        print("已生效。如需回滚: python -m app.scripts.promote_skill "
              f"{args.skill_name} --rollback")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_promote_skill.py -v`
Expected: PASS（13 passed）

- [ ] **Step 5: 人工验证 CLI 列表模式**

Run: `.venv/Scripts/python.exe -m app.scripts.promote_skill --list`
Expected: 打印现有候选及校验结论（如 `[新建] coupon-lookup: ❌ 引用了未知工具: coupon_query`）；无候选时打印提示语。两种都算通过。

- [ ] **Step 6: 提交**

```bash
git add app/scripts/promote_skill.py tests/test_promote_skill.py
git commit -m "feat(skill): 转正/回滚 CLI(校验+门禁+备份,正式目录唯一写入点)"
```

---

### Task 10: 离线闭环入口整合（把新采集源接进主流程）

**Files:**
- Modify: `app/scripts/synthesize_skills.py:114-180`（新增 trace 失败源分支）、`app/scripts/synthesize_skills.py:182-237`（`main`）
- Test: `tests/test_synth_loop_traces.py`

**Interfaces:**
- Consumes: `collect_failures_by_skill`（Task 5）、`synthesize_from_golden`（Task 6）、`validate_candidate`（Task 3）、`Database.list_skill_traces`（Task 1）
- Produces:
  - `run_improvements_from_traces(client, model, traces, archives, skills_dir, out_dir, known_tools=None) -> list[Path]`

> 旧的 `run_improvements`（关键词启发式）**保留不动**，其单测继续跑。`main()` 优先用 trace 版；`skill_traces` 为空（例如刚升级、还没积累轨迹）时自动回退旧路径，保证老库也能用。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_synth_loop_traces.py`：

```python
"""闭环入口:优先用真实轨迹做失败自改进,无轨迹时回退关键词启发式。"""

from app.scripts.synthesize_skills import run_improvements_from_traces
from tests.test_skill_synth import FakeClient

LIVE_MD = """---
name: process-return
description: 退货处理流程,关键词:退货 退款。
---
现行正文。
"""

IMPROVED_MD = """---
name: process-return
description: 退货处理流程(已按失败案例改进)。
---
第一步：调用 `query_order` 核对订单。
第二步：调用 `apply_refund` 提交退款。
"""


def _definitions(tmp_path):
    d = tmp_path / "definitions"
    (d / "process-return").mkdir(parents=True)
    (d / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")
    return str(d)


def _trace(session_id, skill, outcome):
    return {"session_id": session_id, "skill_name": skill, "outcome": outcome, "tool_calls": []}


def _archive(session_id, text):
    return {"session_id": session_id, "user_id": "u1", "summary": "",
            "messages": [{"role": "user", "content": text},
                         {"role": "assistant", "content": "抱歉,抱歉"}]}


def test_improves_skill_from_failed_traces(tmp_path):
    definitions = _definitions(tmp_path)
    out_dir = tmp_path / "candidates"
    client = FakeClient([IMPROVED_MD])

    paths = run_improvements_from_traces(
        client, "test-model",
        traces=[_trace("s1", "process-return", "handoff")],
        archives=[_archive("s1", "退款一直没人处理")],
        skills_dir=definitions, out_dir=str(out_dir),
    )

    assert len(paths) == 1
    assert paths[0].read_text(encoding="utf-8") == IMPROVED_MD
    assert len(client.calls) == 1


def test_no_failed_traces_no_llm_call(tmp_path):
    definitions = _definitions(tmp_path)
    client = FakeClient([])

    paths = run_improvements_from_traces(
        client, "test-model",
        traces=[_trace("s1", "process-return", "success")],
        archives=[_archive("s1", "怎么退货")],
        skills_dir=definitions, out_dir=str(tmp_path / "c"),
    )

    assert paths == []
    assert client.calls == []


def test_trace_for_unknown_skill_is_ignored(tmp_path):
    """轨迹指向正式库里已不存在的 skill(已删)→ 跳过,不调 LLM。"""
    definitions = _definitions(tmp_path)
    client = FakeClient([])

    paths = run_improvements_from_traces(
        client, "test-model",
        traces=[_trace("s1", "deleted-skill", "handoff")],
        archives=[_archive("s1", "随便问问")],
        skills_dir=definitions, out_dir=str(tmp_path / "c"),
    )

    assert paths == []
    assert client.calls == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_synth_loop_traces.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_improvements_from_traces'`

- [ ] **Step 3: 新增 trace 版失败自改进**

在 `app/scripts/synthesize_skills.py` 的 `run_improvements` 函数之后追加：

```python
def run_improvements_from_traces(
    client, model: str, traces: list[dict], archives: list[dict],
    skills_dir: str, out_dir: str, known_tools: set[str] | None = None,
) -> list[Path]:
    """步骤③(G3 升级版):按真实执行轨迹挑失败样本,给对应 skill 产改进候选。

    与关键词启发式版 `run_improvements` 的区别:这里的"某 skill 没搞定"是
    `skill_traces` 里的事实(该轮确实加载了它且结局为转人工/工具失败),不是猜的。

    - 无失败轨迹 → 不调 LLM,返回 [];
    - 轨迹指向正式库里已不存在的 skill → 跳过;
    - 改进候选只写 out_dir(候选目录),绝不碰正式目录。
    """
    from app.agent.skills.failure_cases import collect_failures_by_skill

    grouped = collect_failures_by_skill(traces, archives)
    if not grouped:
        return []

    out_paths: list[Path] = []
    for skill_name, cases in grouped.items():
        content = _read_skill_content(skills_dir, skill_name)
        if content is None:
            continue   # 正式库已无此 skill(被删/改名),跳过
        result = improve_skill(
            client, model, {"name": skill_name, "content": content}, cases, out_dir,
            known_tools=known_tools,
        )
        if result is not None:
            out_paths.append(result)
    return out_paths
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_synth_loop_traces.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 改造 main() 接入新采集源与校验摘要**

在 `app/scripts/synthesize_skills.py` 的 `main()` 中，把从 `candidate_paths: list[Path] = []` 到函数末尾的整段替换为：

```python
    candidate_paths: list[Path] = []
    try:
        candidate_paths = synthesize_skills(client, model, samples, out_dir=CANDIDATES_DIR)
    except Exception as exc:
        print(f"② 聚类创建失败，跳过本步: {exc}")

    # ②b 金牌客服蒸馏(G5):从人工接管过的会话学人的处理经验
    golden_paths: list[Path] = []
    try:
        from app.agent.skills.golden_corpus import synthesize_from_golden
        golden_paths = synthesize_from_golden(client, model, samples, out_dir=CANDIDATES_DIR)
    except Exception as exc:
        print(f"②b 金牌客服蒸馏失败，跳过本步: {exc}")

    # ③ 失败自改进:优先用真实执行轨迹(G3);无轨迹的老库回退关键词启发式
    improve_paths: list[Path] = []
    try:
        traces = get_db().list_skill_traces(
            outcomes=["handoff", "tool_error"], limit=limit * 4,
        )
        if traces:
            improve_paths = run_improvements_from_traces(
                client, model, traces, samples,
                skills_dir=DEFINITIONS_DIR, out_dir=CANDIDATES_DIR,
            )
            print(f"③ 失败自改进:读到 {len(traces)} 条失败轨迹(按真实轨迹关联)")
        else:
            improve_paths = run_improvements(
                client, model, samples, skills_dir=DEFINITIONS_DIR, out_dir=CANDIDATES_DIR,
            )
            print("③ 失败自改进:暂无执行轨迹,回退关键词启发式")
    except Exception as exc:
        print(f"③ 失败自改进失败，跳过本步: {exc}")

    print("\n===== 离线闭环摘要 =====")
    print(f"用户建模: {len(user_tags)} 个用户，标签 {user_tags}")
    print(f"新候选 skill: {[p.parent.name for p in candidate_paths]}")
    print(f"金牌蒸馏候选: {[p.parent.name for p in golden_paths]}")
    print(f"改进候选: {[p.parent.name for p in improve_paths]}")

    # 校验摘要:候选已在合成时过校验,这里再打一次结论供人工审核决策
    from app.scripts.promote_skill import list_candidates
    print("\n===== 候选校验结论 =====")
    items = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
    if not items:
        print("(无候选)")
    for item in items:
        kind = "改进" if item["is_improvement"] else "新建"
        status = "✅ 可送门禁" if item["valid"] else f"❌ {'; '.join(item['errors'])}"
        print(f"[{kind}] {item['name']}: {status}")

    print("\n候选不会被 SkillManager 自动加载。转正需过门禁:")
    print("  python -m app.scripts.promote_skill --list")
    print("  python -m app.scripts.promote_skill <skill-name>")
```

- [ ] **Step 6: 回归既有闭环测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_synth_loop.py tests/test_synth_loop_traces.py tests/test_skill_synth.py -v`
Expected: PASS（全绿；旧 `run_improvements` 未改动，其测试应仍通过）

- [ ] **Step 7: 提交**

```bash
git add app/scripts/synthesize_skills.py tests/test_synth_loop_traces.py
git commit -m "feat(skill): 闭环入口接入轨迹失败源/金牌蒸馏/校验摘要"
```

---

### Task 11: 候选与门禁状态的管理端接口

**Files:**
- Modify: `app/api/app.py`（在 `handoffs` 路由之前插入新端点）
- Test: `tests/test_skill_admin_api.py`

**Interfaces:**
- Consumes: `list_candidates(candidates_dir, definitions_dir)`（Task 9）、`Database.list_skill_traces`（Task 1）、`SkillManager`（既有）
- Produces:
  - `GET /api/admin/skills`（需 admin 鉴权）→ `{"live": [{"name","description"}], "candidates": [...], "traces": {"<skill>": {"success": n, "handoff": n, "tool_error": n}}}`

> 让自进化闭环在管理端**可见**：现行技能、待审候选（含校验结论）、每个 skill 的实战成功/失败分布。前端 UI 不在本计划范围。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_admin_api.py`：

```python
"""管理端暴露自进化状态:现行技能 / 待审候选(含校验结论) / 各 skill 实战结局分布。"""

from fastapi.testclient import TestClient

from app.api.app import create_app


def _client():
    return TestClient(create_app())


def _headers():
    """admin_token 为空时后端不鉴权(见 app/hardening/auth.py),此时不必带头。"""
    from app.config.settings import settings
    return {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}


def test_admin_skills_returns_live_and_candidates():
    resp = _client().get("/api/admin/skills", headers=_headers())
    assert resp.status_code == 200

    data = resp.json()
    assert "live" in data and "candidates" in data and "traces" in data
    assert isinstance(data["live"], list)
    live_names = {s["name"] for s in data["live"]}
    assert "process-return" in live_names          # 正式库里的技能
    for skill in data["live"]:
        assert skill["description"]


def test_candidate_entries_carry_validation_verdict():
    data = _client().get("/api/admin/skills", headers=_headers()).json()
    for item in data["candidates"]:
        assert set(item) >= {"name", "valid", "unknown_tools", "errors", "is_improvement"}
        assert isinstance(item["valid"], bool)


def test_traces_map_counts_outcomes(tmp_path, monkeypatch):
    from app.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    db.record_skill_trace("s1", "u1", "process-return", [], "success")
    db.record_skill_trace("s2", "u1", "process-return", [], "handoff")
    db.record_skill_trace("s3", "u1", "process-return", [], "handoff")
    monkeypatch.setattr("app.db.get_db", lambda: db)

    data = _client().get("/api/admin/skills", headers=_headers()).json()
    counts = data["traces"]["process-return"]
    assert counts["success"] == 1
    assert counts["handoff"] == 2
    assert counts.get("tool_error", 0) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_admin_api.py -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 实现端点**

在 `app/api/app.py` 中，把：

```python
    @app.get("/api/handoffs", dependencies=[Depends(admin_auth)])
    def handoffs():
```

改为：

```python
    @app.get("/api/admin/skills", dependencies=[Depends(admin_auth)])
    def admin_skills():
        """自进化状态总览:现行技能 / 待审候选(含校验结论) / 各 skill 实战结局分布。

        candidates 只读 _candidates 目录,绝不在此处转正——转正只走
        app/scripts/promote_skill.py(校验+门禁+备份)。
        """
        from app.agent.skills.loader import SkillManager
        from app.scripts.promote_skill import CANDIDATES_DIR, DEFINITIONS_DIR, list_candidates

        live = SkillManager(skills_dir=settings.skills_dir, enabled=True).get_catalog()

        try:
            candidates = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
        except Exception:  # noqa: BLE001 候选目录异常不该让总览 500
            candidates = []

        traces: dict[str, dict[str, int]] = {}
        try:
            for row in get_db().list_skill_traces(limit=500):
                bucket = traces.setdefault(row["skill_name"], {})
                outcome = row.get("outcome") or "unknown"
                bucket[outcome] = bucket.get(outcome, 0) + 1
        except Exception:  # noqa: BLE001
            traces = {}

        return {"live": live, "candidates": candidates, "traces": traces}

    @app.get("/api/handoffs", dependencies=[Depends(admin_auth)])
    def handoffs():
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_admin_api.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS（无新增失败；若有既有失败，与实施前 `git stash` 基线对比确认非本次引入）

- [ ] **Step 6: 提交**

```bash
git add app/api/app.py tests/test_skill_admin_api.py
git commit -m "feat(api): 管理端暴露自进化状态(现行技能/待审候选/实战结局分布)"
```

---

### Task 12: 风险分级与放行策略（分级授权）

**Files:**
- Create: `app/agent/skills/risk.py`
- Test: `tests/test_skill_risk.py`

**Interfaces:**
- Consumes: `referenced_tools(content)`（Task 3）
- Produces:
  - 常量 `RISK_HIGH` / `RISK_MEDIUM` / `RISK_LOW`
  - 常量 `POLICY_CANARY_AB` / `POLICY_GATE_THEN_WATCH` / `POLICY_MANUAL`
  - `HIGH_RISK_TOOLS: frozenset[str]`、`COMMITMENT_KEYWORDS: tuple[str, ...]`
  - `classify_risk(content: str, is_new_skill: bool = False) -> str`
  - `promotion_policy(risk: str) -> str`
  - `CANARY_PERCENT: int`、`CANARY_MIN_SAMPLES: int`、`ABSOLUTE_MIN_SAMPLES: int`、`ABSOLUTE_MIN_RATE: float`

> **为什么分级**：一刀切人工确认是过度保守（连改个话术都要人批），一刀切自动又会让「自我改写退款流程」直接造成资金风险。按候选**引用的工具**和**是否含承诺类措辞**自动判档——纯静态判定，转正前就能算出来，不用跑起来。
>
> **三档对应三种放行方式**：
> - `low`（改进已有 skill 且只用只读工具）→ `POLICY_CANARY_AB`：真 A/B 灰度，与现行版比成功率，不劣化即自动转正。
> - `medium`（全新 skill——线上原本没有它，**没有对照组**可比）→ `POLICY_GATE_THEN_WATCH`：过离线门禁即转正，之后按绝对成功率看门狗守着。
> - `high`（引用退款/议价/取消/改址/开票/催发货，或正文承诺免运费/赔付）→ `POLICY_MANUAL`：永远人工，代码不得自动转正。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_risk.py`：

```python
"""分级授权:按引用工具与承诺措辞自动判风险档,决定放行方式。"""

from app.agent.skills.risk import (
    POLICY_CANARY_AB,
    POLICY_GATE_THEN_WATCH,
    POLICY_MANUAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    classify_risk,
    promotion_policy,
)

READONLY_MD = """---
name: order-query-all
description: 查全部订单。
---
第一步：调用 `list_user_orders`，需要明细再 `query_order`。
"""

REFUND_MD = """---
name: process-return
description: 退货处理。
---
第一步：`query_order` 核对。第二步：`apply_refund` 提交退款。
"""

BARGAIN_MD = """---
name: bargain-flow
description: 议价。
---
第一步：`query_product`。第二步：`negotiate_price` 还价。
"""

COMMITMENT_MD = """---
name: shipping-care
description: 物流关怀。
---
第一步：`query_logistics` 查轨迹。若超时，直接告知买家本单免运费。
"""


# ---------- classify_risk ----------

def test_readonly_improvement_is_low_risk():
    assert classify_risk(READONLY_MD, is_new_skill=False) == RISK_LOW


def test_readonly_new_skill_is_medium_risk():
    """全新 skill 引入新行为,线上没有对照组,至少中档。"""
    assert classify_risk(READONLY_MD, is_new_skill=True) == RISK_MEDIUM


def test_refund_tool_is_high_risk():
    assert classify_risk(REFUND_MD) == RISK_HIGH


def test_negotiate_price_is_high_risk():
    assert classify_risk(BARGAIN_MD) == RISK_HIGH


def test_commitment_wording_is_high_risk_even_without_write_tool():
    """只读工具 + 承诺免运费 → 仍是高危(承诺有资金后果)。"""
    assert classify_risk(COMMITMENT_MD) == RISK_HIGH


def test_high_risk_wins_over_new_skill_flag():
    assert classify_risk(REFUND_MD, is_new_skill=True) == RISK_HIGH


# ---------- promotion_policy ----------

def test_policy_per_risk_tier():
    assert promotion_policy(RISK_LOW) == POLICY_CANARY_AB
    assert promotion_policy(RISK_MEDIUM) == POLICY_GATE_THEN_WATCH
    assert promotion_policy(RISK_HIGH) == POLICY_MANUAL


def test_unknown_risk_falls_back_to_manual():
    """未知档位一律按最保守处理(fail-closed)。"""
    assert promotion_policy("whatever") == POLICY_MANUAL
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_risk.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.risk'`

- [ ] **Step 3: 实现 risk.py**

创建 `app/agent/skills/risk.py`：

```python
"""分级授权:按候选引用的工具与承诺类措辞自动判风险档,决定放行方式。

一刀切人工确认过度保守(改个话术也要人批),一刀切自动又会让"自我改写退款流程"
直接造成资金风险。所以按静态特征分三档:

- low    改进已有 skill 且只用只读工具 → 真 A/B 灰度,不劣化即自动转正;
- medium 全新 skill(线上原本没有它,**没有对照组**) → 过离线门禁即转正,之后按
         绝对成功率看门狗守着;
- high   引用退款/议价/取消/改址/开票/催发货,或正文承诺免运费/赔付 → 永远人工。

判定只看静态文本,不需要跑起来,故可在转正前离线算出。
"""

from __future__ import annotations

from app.agent.skills.validator import referenced_tools

RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"

POLICY_CANARY_AB = "canary_ab"              # 灰度 A/B:与现行版比成功率
POLICY_GATE_THEN_WATCH = "gate_then_watch"  # 过离线门禁即转正 + 绝对成功率看门狗
POLICY_MANUAL = "manual"                    # 永远人工确认

# 碰钱/改单/开票类工具:引用即高危——自我改写这些流程会直接造成资金与承诺风险
HIGH_RISK_TOOLS = frozenset({
    "apply_refund", "cancel_order", "change_address",
    "negotiate_price", "issue_invoice", "expedite_shipping",
})

# 承诺类措辞:即使只引用了只读工具,正文承诺免运费/赔付一样有资金后果
COMMITMENT_KEYWORDS = (
    "免运费", "免邮", "赔付", "补偿", "全额退", "承担运费", "包退", "先行赔付",
)

# 灰度 A/B 参数(low 档)
CANARY_PERCENT = 50
CANARY_MIN_SAMPLES = 10
CANARY_MAX_DROP = 0.1

# 绝对成功率看门狗参数(medium 档转正后)
ABSOLUTE_MIN_SAMPLES = 30
ABSOLUTE_MIN_RATE = 0.6


def classify_risk(content: str, is_new_skill: bool = False) -> str:
    """判定候选风险档(high/medium/low)。规则按顺序命中即返回。"""
    if referenced_tools(content) & HIGH_RISK_TOOLS:
        return RISK_HIGH
    if any(kw in content for kw in COMMITMENT_KEYWORDS):
        return RISK_HIGH
    if is_new_skill:
        return RISK_MEDIUM
    return RISK_LOW


def promotion_policy(risk: str) -> str:
    """该档的放行方式;未知档位一律按最保守的人工处理(fail-closed)。"""
    return {
        RISK_LOW: POLICY_CANARY_AB,
        RISK_MEDIUM: POLICY_GATE_THEN_WATCH,
    }.get(risk, POLICY_MANUAL)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_risk.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add app/agent/skills/risk.py tests/test_skill_risk.py
git commit -m "feat(skill): 风险分级与放行策略(碰钱/承诺类永远人工,只读改进走灰度)"
```

---

### Task 13: 灰度路由（候选按会话接管部分真实流量）

**Files:**
- Create: `app/agent/skills/canary.py`
- Modify: `app/db/database.py`（`init_schema` 加 `skill_canaries` 表 + `skill_traces.variant` 列迁移；`record_skill_trace` 加 `variant` 参数；新增 4 个 canary 方法）
- Modify: `app/agent/skills/loader.py:135-153`（`load_skill` 灰度路由 + 返回 variant）
- Modify: `app/agent/skills/execution_trace.py`（`SkillTurn.variant`）
- Modify: `app/agent/chat.py`（`_record_skill_turn` 透传 variant）
- Modify: `app/config/settings.py`
- Test: `tests/test_skill_canary.py`

**Interfaces:**
- Consumes: `Database.record_skill_trace`（Task 1）、`SkillTurn`（Task 2）
- Produces:
  - `canary.VARIANT_LIVE` / `canary.VARIANT_CANARY`
  - `canary.in_canary_bucket(skill_name: str, session_id: str, percent: int) -> bool`
  - `Database.start_canary(skill_name, candidate_path, percent, risk, policy) -> None`
  - `Database.get_active_canary(skill_name) -> dict | None`
  - `Database.list_active_canaries() -> list[dict]`
  - `Database.finish_canary(skill_name, status) -> bool`
  - `Database.record_skill_trace(..., variant: str = "live")`
  - `SkillManager.load_skill(skill_name)` 返回值新增 `"variant"` 键
  - `SkillTurn.variant: str`

> **为什么按 session 哈希而不是随机数**：同一通对话必须始终看到同一个版本，否则顾客会在一次会话里被两套流程处理。哈希是确定性的，重启与多实例结果一致。
>
> **fail-soft**：开关关闭、取不到会话、DB 异常、候选文件缺失——任何一项都退回正式版本。灰度是增强，绝不能因为它让 `load_skill` 失败。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_canary.py`：

```python
"""灰度路由:候选按会话哈希接管部分流量,与现行版 A/B;任何异常退回正式版。"""

import json

from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE, in_canary_bucket
from app.agent.skills.loader import SkillManager
from app.db import Database

LIVE_MD = """---
name: process-return
description: 退货处理(现行版)。
---
现行正文。
"""

CANDIDATE_MD = """---
name: process-return
description: 退货处理(候选版)。
---
候选正文。
"""


def _definitions(tmp_path):
    d = tmp_path / "definitions"
    (d / "process-return").mkdir(parents=True)
    (d / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")
    return d


def _candidate(tmp_path):
    p = tmp_path / "candidates" / "process-return" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(CANDIDATE_MD, encoding="utf-8")
    return p


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


# ---------- in_canary_bucket ----------

def test_bucket_zero_percent_never_hits():
    assert in_canary_bucket("s", "sess-1", 0) is False


def test_bucket_hundred_percent_always_hits():
    assert in_canary_bucket("s", "sess-1", 100) is True


def test_bucket_is_deterministic_per_session():
    """同一会话反复判定结果不变(一通对话不能中途换版本)。"""
    first = in_canary_bucket("process-return", "sess-42", 50)
    for _ in range(5):
        assert in_canary_bucket("process-return", "sess-42", 50) is first


def test_bucket_splits_traffic_roughly_by_percent():
    hits = sum(1 for i in range(400) if in_canary_bucket("process-return", f"sess-{i}", 25))
    assert 50 < hits < 150   # 25% of 400 = 100,允许哈希抖动


# ---------- DB 灰度登记 ----------

def test_start_and_get_active_canary(tmp_path):
    db = _db(tmp_path)
    db.start_canary("process-return", "/path/SKILL.md", 50, "low", "canary_ab")

    row = db.get_active_canary("process-return")
    assert row["percent"] == 50
    assert row["risk"] == "low"
    assert row["policy"] == "canary_ab"
    assert row["status"] == "active"
    assert db.get_active_canary("track-order") is None


def test_start_canary_supersedes_previous(tmp_path):
    db = _db(tmp_path)
    db.start_canary("process-return", "/old.md", 10, "low", "canary_ab")
    db.start_canary("process-return", "/new.md", 50, "low", "canary_ab")

    assert db.get_active_canary("process-return")["candidate_path"] == "/new.md"
    assert len(db.list_active_canaries()) == 1   # 同 skill 同时只有一个活跃灰度


def test_finish_canary_marks_status(tmp_path):
    db = _db(tmp_path)
    db.start_canary("process-return", "/p.md", 50, "low", "canary_ab")

    assert db.finish_canary("process-return", "promoted") is True
    assert db.get_active_canary("process-return") is None
    assert db.finish_canary("process-return", "promoted") is False   # 已无活跃记录


def test_record_skill_trace_stores_variant(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace("s1", "u1", "process-return", [], "success", variant=VARIANT_CANARY)
    db.record_skill_trace("s2", "u1", "process-return", [], "success")   # 默认 live

    rows = {r["session_id"]: r for r in db.list_skill_traces()}
    assert rows["s1"]["variant"] == VARIANT_CANARY
    assert rows["s2"]["variant"] == VARIANT_LIVE


# ---------- load_skill 路由 ----------

def test_load_skill_returns_live_variant_without_canary(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    db = _db(tmp_path)
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")

    assert result["success"] is True
    assert result["variant"] == VARIANT_LIVE
    assert "现行正文" in result["instructions"]


def test_load_skill_serves_candidate_when_session_in_bucket(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    candidate = _candidate(tmp_path)
    db = _db(tmp_path)
    db.start_canary("process-return", str(candidate), 100, "low", "canary_ab")   # 100% 必中
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")

    assert result["variant"] == VARIANT_CANARY
    assert "候选正文" in result["instructions"]


def test_load_skill_falls_back_when_canary_disabled(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    candidate = _candidate(tmp_path)
    db = _db(tmp_path)
    db.start_canary("process-return", str(candidate), 100, "low", "canary_ab")
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", False)   # 总开关关
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")
    assert result["variant"] == VARIANT_LIVE
    assert "现行正文" in result["instructions"]


def test_load_skill_falls_back_when_candidate_file_missing(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    db = _db(tmp_path)
    db.start_canary("process-return", str(tmp_path / "gone" / "SKILL.md"), 100, "low", "canary_ab")
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")
    assert result["variant"] == VARIANT_LIVE   # 候选文件没了 → 退回正式版,不报错


def test_load_skill_falls_back_when_db_raises(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.db.get_db", boom)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")
    assert result["success"] is True            # 灰度炸了不能让 load_skill 失败
    assert result["variant"] == VARIANT_LIVE


# ---------- SkillTurn 记住 variant ----------

def test_skill_turn_captures_variant_from_load_result():
    from app.agent.skills.execution_trace import SkillTurn

    turn = SkillTurn()
    turn.note_tool_call("load_skill", json.dumps(
        {"success": True, "skill_name": "process-return", "instructions": "x",
         "variant": VARIANT_CANARY}, ensure_ascii=False))

    assert turn.skill_name == "process-return"
    assert turn.variant == VARIANT_CANARY


def test_skill_turn_variant_defaults_to_live():
    from app.agent.skills.execution_trace import SkillTurn

    turn = SkillTurn()
    turn.note_tool_call("load_skill", json.dumps(
        {"success": True, "skill_name": "process-return", "instructions": "x"},
        ensure_ascii=False))

    assert turn.variant == VARIANT_LIVE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_canary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.canary'`

- [ ] **Step 3: 实现 canary.py**

创建 `app/agent/skills/canary.py`：

```python
"""灰度路由:让候选 skill 按会话哈希接管一部分真实流量,与现行版本 A/B。

为什么按 session 哈希而不是随机数:同一通对话必须始终看到同一个版本,否则顾客
会在一次会话里被两套流程处理。哈希是确定性的,进程重启/多实例部署结果一致。
"""

from __future__ import annotations

import hashlib

VARIANT_LIVE = "live"
VARIANT_CANARY = "canary"


def in_canary_bucket(skill_name: str, session_id: str, percent: int) -> bool:
    """该会话是否落进这个 skill 的灰度桶。percent<=0 恒 False,>=100 恒 True。

    哈希里带 skill_name:同一会话在不同 skill 上的分桶相互独立,避免"某个会话
    永远是灰度组"这种系统性偏斜。
    """
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.md5(f"{skill_name}:{session_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < percent
```

- [ ] **Step 4: DB —— 建表、变体列迁移、canary 方法**

其一，在 `app/db/database.py` 的 `init_schema` 里，把 Task 1 加的 `idx_skill_traces_name` 索引那一行之后、`"""` 之前，插入：

```sql
                CREATE TABLE IF NOT EXISTS skill_canaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name TEXT NOT NULL,
                    candidate_path TEXT NOT NULL,
                    percent INTEGER NOT NULL,
                    risk TEXT,
                    policy TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_skill_canaries_active
                    ON skill_canaries(skill_name, status);
```

其二，在 `init_schema` 的兼容迁移段里（`conversations` 补 `updated_at` 那一段之后、最后的 `conn.commit()` 之前）插入：

```python
            # 兼容旧库：skill_traces 补 variant 列(灰度 A/B 需区分 live/canary)
            stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)").fetchall()}
            if "variant" not in stcols:
                conn.execute("ALTER TABLE skill_traces ADD COLUMN variant TEXT DEFAULT 'live'")
                conn.execute("UPDATE skill_traces SET variant = 'live' WHERE variant IS NULL")
                conn.commit()
```

其三，把 Task 1 加的 `record_skill_trace` 整个方法替换为：

```python
    def record_skill_trace(self, session_id: str, user_id: str, skill_name: str,
                           tool_calls: list[dict], outcome: str,
                           variant: str = "live") -> None:
        """记录一轮 skill 执行轨迹。variant 区分现行版/灰度候选,供 A/B 判定。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
                "outcome, created_at, variant) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, skill_name,
                 json.dumps(tool_calls or [], ensure_ascii=False), outcome,
                 self._now(), variant),
            )
            conn.commit()
        finally:
            conn.close()
```

其四，在 `list_skill_traces` 之后追加灰度登记方法：

```python
    # ---------- Skill 灰度登记(分级授权:候选按会话接管部分流量) ----------
    def start_canary(self, skill_name: str, candidate_path: str, percent: int,
                     risk: str, policy: str) -> None:
        """登记一个活跃灰度。同 skill 已有活跃记录先置 superseded,保证同时只有一个。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE skill_canaries SET status = 'superseded', finished_at = ? "
                "WHERE skill_name = ? AND status = 'active'", (self._now(), skill_name))
            conn.execute(
                "INSERT INTO skill_canaries (skill_name, candidate_path, percent, risk, "
                "policy, status, started_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (skill_name, candidate_path, percent, risk, policy, self._now()))
            conn.commit()
        finally:
            conn.close()

    def get_active_canary(self, skill_name: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM skill_canaries WHERE skill_name = ? AND status = 'active' "
                "ORDER BY id DESC LIMIT 1", (skill_name,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_active_canaries(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM skill_canaries WHERE status = 'active' "
                "ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def finish_canary(self, skill_name: str, status: str) -> bool:
        """结束该 skill 的活跃灰度(status: promoted / rolled_back)。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE skill_canaries SET status = ?, finished_at = ? "
                "WHERE skill_name = ? AND status = 'active'",
                (status, self._now(), skill_name))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
```

- [ ] **Step 5: 加配置开关**

在 `app/config/settings.py` 的 `skill_gate_tolerance` 那一行之后追加：

```python
    skill_canary_enabled: bool = True   # 灰度路由总开关(关=永远只加载正式版本)
```

在 `.env.example` 的 `SKILL_GATE_TOLERANCE=0.05` 之后追加：

```
SKILL_CANARY_ENABLED=true
```

- [ ] **Step 6: loader 灰度路由**

在 `app/agent/skills/loader.py` 中，把 `load_skill` 方法替换为：

```python
    def _canary_body(self, skill_name: str) -> str | None:
        """灰度路由:该 skill 有活跃候选且当前会话落桶 → 返回候选正文,否则 None。

        fail-soft:开关关闭、取不到会话、DB 异常、候选文件缺失都返回 None(退回
        正式版)。灰度是增强,绝不能因为它让 load_skill 失败。
        """
        from app.config.settings import settings

        if not getattr(settings, "skill_canary_enabled", False):
            return None
        try:
            from app.agent.skills.canary import in_canary_bucket
            from app.agent.tools.bargain import get_current_session
            from app.db import get_db

            session_id = get_current_session()
            if not session_id:
                return None
            canary = get_db().get_active_canary(skill_name)
            if not canary:
                return None
            if not in_canary_bucket(skill_name, session_id, int(canary["percent"])):
                return None
            path = Path(canary["candidate_path"])
            if not path.exists():
                return None
            return _parse_body(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 灰度任何异常都退回正式版
            return None

    def load_skill(self, skill_name: str) -> dict:
        """加载指定 skill 的完整指令。供 load_skill 工具调用。

        返回值带 variant(live/canary):灰度期本会话拿到的是哪个版本,由调用方
        记进执行轨迹,供看门狗做 A/B 判定。
        """
        if not self.enabled:
            return {"success": False, "error": "技能系统未启用"}

        skill = self._skills.get(skill_name)
        if not skill:
            available = ", ".join(self._skills.keys()) or "无"
            return {
                "success": False,
                "error": f"未找到技能「{skill_name}」，可用技能：{available}",
            }

        from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE

        body = skill.load_body()
        variant = VARIANT_LIVE
        canary_body = self._canary_body(skill_name)
        if canary_body is not None:
            body = canary_body
            variant = VARIANT_CANARY

        return {
            "success": True,
            "skill_name": skill.name,
            "instructions": body,
            "variant": variant,
        }
```

- [ ] **Step 7: SkillTurn 记住 variant**

在 `app/agent/skills/execution_trace.py` 中，把 `_loaded_skill_name` 之后、`@dataclass` 之前插入：

```python
def _loaded_variant(result_str: str) -> str:
    """从 load_skill 返回里取本轮实际加载的版本(live/canary);缺失按 live。"""
    from app.agent.skills.canary import VARIANT_LIVE

    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return VARIANT_LIVE
    if not isinstance(data, dict):
        return VARIANT_LIVE
    return str(data.get("variant") or VARIANT_LIVE)
```

把 `SkillTurn` 的字段与 `note_tool_call` 替换为：

```python
@dataclass
class SkillTurn:
    """单轮对话的 skill 执行轨迹。未加载 skill 的轮次不会被落库(has_skill=False)。"""

    skill_name: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    variant: str = "live"   # 本轮实际加载的版本(灰度期可能是 canary)

    def note_tool_call(self, name: str, result_str: str) -> None:
        """记录一次工具调用。若是成功的 load_skill,同时记下 skill 名与加载的版本。"""
        ok, error = _parse_result(result_str)
        if name == LOAD_SKILL_TOOL and ok:
            loaded = _loaded_skill_name(result_str)
            if loaded:
                self.skill_name = loaded
                self.variant = _loaded_variant(result_str)
        self.tool_calls.append({"name": name, "ok": ok, "error": error})
```

- [ ] **Step 8: chat.py 透传 variant**

在 `app/agent/chat.py` 的 `_record_skill_turn` 中，把：

```python
                skill_name=turn.skill_name, tool_calls=turn.tool_calls,
                outcome=turn.outcome(result.requires_human),
```

改为：

```python
                skill_name=turn.skill_name, tool_calls=turn.tool_calls,
                outcome=turn.outcome(result.requires_human),
                variant=turn.variant,
```

- [ ] **Step 9: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_canary.py -v`
Expected: PASS（15 passed）

- [ ] **Step 10: 回归 skill / trace / DB 测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skills.py tests/test_skill_trace_db.py tests/test_skill_execution_trace.py tests/test_skill_synth.py tests/test_db_schema.py -v`
Expected: PASS。

若 `tests/test_skills.py` 里有断言 `load_skill` 返回值**完全等于**某个 dict（因为新增了 `variant` 键而失败），把断言改为逐键校验，例如 `assert result["success"] is True and result["skill_name"] == "..."`。

- [ ] **Step 11: 提交**

```bash
git add app/agent/skills/canary.py app/agent/skills/loader.py app/agent/skills/execution_trace.py app/agent/chat.py app/db/database.py app/config/settings.py .env.example tests/test_skill_canary.py tests/test_skills.py
git commit -m "feat(skill): 灰度路由(候选按会话哈希接管部分流量,轨迹记 variant)"
```

---

### Task 14: 看门狗与自动转正/回滚（在线自进化收口）

**Files:**
- Create: `app/agent/skills/watchdog.py`
- Create: `app/scripts/skill_watchdog.py`
- Test: `tests/test_skill_watchdog.py`

**Interfaces:**
- Consumes: `VARIANT_LIVE` / `VARIANT_CANARY`（Task 13）、`OUTCOME_SUCCESS`（Task 2）、`classify_risk` / `promotion_policy` / 各阈值常量（Task 12）、`promote` / `rollback` / `list_candidates`（Task 9）、`gate_candidate` / `gate_case_ids` / `default_eval_fn`（Task 8）、`Database` 灰度方法（Task 13）
- Produces:
  - 常量 `DECISION_PROMOTE` / `DECISION_ROLLBACK` / `DECISION_WAIT`
  - `success_rate(rows: list[dict]) -> float | None`
  - `evaluate_ab(traces: list[dict], min_samples: int = 10, max_drop: float = 0.1) -> dict`
  - `evaluate_absolute(traces: list[dict], min_samples: int = 30, min_rate: float = 0.6) -> dict`
    两者都返回 `{"decision", "reason", "live_rate", "canary_rate", "live_samples", "canary_samples"}`（`evaluate_absolute` 把被观测版本填进 `canary_*`，`live_*` 为 None/0）

> **这一步才让「在线自进化」成立**：低风险候选灰度跑赢就自动转正、跑输就自动回滚，全程无人。高风险候选只被列出，代码绝不自动调 `promote`。
>
> `--check` 自动转正时用 `force=True`——不是"绕过检查"，而是**灰度实战数据比离线评测是更强的证据**；`promote` 内部的工具名校验依然会跑，一样挡不住的编造工具名不会因为 force 上线。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_watchdog.py`：

```python
"""看门狗:按实战轨迹判定灰度候选该转正、该回滚还是继续观察。纯函数,不碰 DB。"""

from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE
from app.agent.skills.watchdog import (
    DECISION_PROMOTE,
    DECISION_ROLLBACK,
    DECISION_WAIT,
    evaluate_ab,
    evaluate_absolute,
    success_rate,
)


def _rows(variant, n_success, n_fail):
    return ([{"variant": variant, "outcome": "success"}] * n_success
            + [{"variant": variant, "outcome": "handoff"}] * n_fail)


# ---------- success_rate ----------

def test_success_rate_basic():
    assert success_rate(_rows(VARIANT_LIVE, 3, 1)) == 0.75


def test_success_rate_empty_is_none():
    assert success_rate([]) is None


# ---------- evaluate_ab ----------

def test_ab_waits_until_enough_canary_samples():
    traces = _rows(VARIANT_LIVE, 50, 0) + _rows(VARIANT_CANARY, 3, 0)
    result = evaluate_ab(traces, min_samples=10)
    assert result["decision"] == DECISION_WAIT
    assert "样本不足" in result["reason"]
    assert result["canary_samples"] == 3


def test_ab_promotes_when_candidate_not_worse():
    traces = _rows(VARIANT_LIVE, 6, 4) + _rows(VARIANT_CANARY, 9, 1)
    result = evaluate_ab(traces, min_samples=10, max_drop=0.1)
    assert result["decision"] == DECISION_PROMOTE
    assert result["live_rate"] == 0.6
    assert result["canary_rate"] == 0.9


def test_ab_rolls_back_when_candidate_worse_than_tolerance():
    traces = _rows(VARIANT_LIVE, 9, 1) + _rows(VARIANT_CANARY, 4, 6)
    result = evaluate_ab(traces, min_samples=10, max_drop=0.1)
    assert result["decision"] == DECISION_ROLLBACK
    assert "低于" in result["reason"]


def test_ab_tolerates_small_dip():
    traces = _rows(VARIANT_LIVE, 10, 0) + _rows(VARIANT_CANARY, 19, 1)
    result = evaluate_ab(traces, min_samples=10, max_drop=0.1)
    assert result["decision"] == DECISION_PROMOTE   # 掉 0.05 < 容差 0.1


def test_ab_waits_without_live_control_group():
    """没有对照组不能用 A/B 判(该走绝对值模式)。"""
    traces = _rows(VARIANT_CANARY, 20, 0)
    result = evaluate_ab(traces, min_samples=10)
    assert result["decision"] == DECISION_WAIT
    assert "对照" in result["reason"]


def test_ab_treats_missing_variant_as_live():
    """Task 13 迁移前的老轨迹没有 variant 字段,按 live 计。"""
    traces = [{"outcome": "success"}] * 10 + _rows(VARIANT_CANARY, 10, 0)
    result = evaluate_ab(traces, min_samples=10)
    assert result["live_samples"] == 10
    assert result["decision"] == DECISION_PROMOTE


# ---------- evaluate_absolute ----------

def test_absolute_waits_until_enough_samples():
    result = evaluate_absolute(_rows(VARIANT_LIVE, 5, 0), min_samples=30)
    assert result["decision"] == DECISION_WAIT


def test_absolute_promotes_when_rate_above_floor():
    result = evaluate_absolute(_rows(VARIANT_LIVE, 25, 5), min_samples=30, min_rate=0.6)
    assert result["decision"] == DECISION_PROMOTE


def test_absolute_rolls_back_when_rate_below_floor():
    result = evaluate_absolute(_rows(VARIANT_LIVE, 9, 21), min_samples=30, min_rate=0.6)
    assert result["decision"] == DECISION_ROLLBACK
    assert "低于下限" in result["reason"]
```

创建 `tests/test_skill_watchdog_cli.py`：

```python
"""看门狗 CLI:高危候选只列出不自动上线;低危候选灰度跑赢自动转正、跑输自动回滚。"""

from app.db import Database
from app.scripts.skill_watchdog import check_canaries, start_for_candidate

LIVE_MD = """---
name: process-return
description: 退货处理(现行版)。
---
第一步：调用 `query_order` 核对。
"""

READONLY_CANDIDATE = """---
name: process-return
description: 退货处理(候选版,只读)。
---
第一步：调用 `query_order` 与 `list_user_orders` 核对。
"""

REFUND_CANDIDATE = """---
name: process-return
description: 退货处理(候选版,含退款)。
---
第一步：调用 `apply_refund` 提交退款。
"""


def _setup(tmp_path, candidate_md):
    definitions = tmp_path / "definitions"
    (definitions / "process-return").mkdir(parents=True)
    (definitions / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")

    candidates = definitions / "_candidates"
    (candidates / "process-return").mkdir(parents=True)
    (candidates / "process-return" / "SKILL.md").write_text(candidate_md, encoding="utf-8")

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return str(definitions), str(candidates), str(definitions / "_archive"), db


def test_start_puts_readonly_improvement_into_canary(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)

    result = start_for_candidate("process-return", definitions, candidates, archive, db)

    assert result["risk"] == "low"
    assert result["action"] == "canary_started"
    row = db.get_active_canary("process-return")
    assert row["percent"] == 50
    assert row["policy"] == "canary_ab"


def test_start_refuses_high_risk_candidate(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, REFUND_CANDIDATE)

    result = start_for_candidate("process-return", definitions, candidates, archive, db)

    assert result["risk"] == "high"
    assert result["action"] == "manual_required"
    assert db.get_active_canary("process-return") is None   # 高危不进灰度
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD       # 正式目录未被改动


def test_check_promotes_winning_canary(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)
    start_for_candidate("process-return", definitions, candidates, archive, db)

    for _ in range(10):
        db.record_skill_trace("s", "u", "process-return", [], "handoff", variant="live")
    for _ in range(12):
        db.record_skill_trace("s", "u", "process-return", [], "success", variant="canary")

    results = check_canaries(definitions, candidates, archive, db)

    assert results[0]["decision"] == "promote"
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == READONLY_CANDIDATE   # 已自动转正
    assert db.get_active_canary("process-return") is None           # 灰度已收口


def test_check_rolls_back_losing_canary(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)
    start_for_candidate("process-return", definitions, candidates, archive, db)

    for _ in range(10):
        db.record_skill_trace("s", "u", "process-return", [], "success", variant="live")
    for _ in range(12):
        db.record_skill_trace("s", "u", "process-return", [], "handoff", variant="canary")

    results = check_canaries(definitions, candidates, archive, db)

    assert results[0]["decision"] == "rollback"
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD    # 正式版没被换掉
    assert db.get_active_canary("process-return") is None  # 灰度已废弃


def test_check_waits_on_insufficient_samples(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)
    start_for_candidate("process-return", definitions, candidates, archive, db)
    db.record_skill_trace("s", "u", "process-return", [], "success", variant="canary")

    results = check_canaries(definitions, candidates, archive, db)

    assert results[0]["decision"] == "wait"
    assert db.get_active_canary("process-return") is not None   # 继续观察
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_watchdog.py tests/test_skill_watchdog_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.watchdog'`

- [ ] **Step 3: 实现 watchdog.py**

创建 `app/agent/skills/watchdog.py`：

```python
"""看门狗:按实战轨迹判定灰度候选该转正、该回滚还是继续观察。

两种模式对应 risk.promotion_policy 的两种放行方式:
- evaluate_ab:改进型候选有对照组(同 skill 的现行版) → 比成功率;
- evaluate_absolute:新建型候选没有对照组(线上原本没这个 skill) → 看绝对成功率。

都是纯函数(入参是 skill_traces 行,不碰 DB),便于单测与离线复算。
"""

from __future__ import annotations

from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE
from app.agent.skills.execution_trace import OUTCOME_SUCCESS

DECISION_PROMOTE = "promote"
DECISION_ROLLBACK = "rollback"
DECISION_WAIT = "wait"


def success_rate(rows: list[dict]) -> float | None:
    """成功轮占比;空样本返回 None(区别于 0.0——"没数据"不等于"全失败")。"""
    if not rows:
        return None
    ok = sum(1 for r in rows if r.get("outcome") == OUTCOME_SUCCESS)
    return ok / len(rows)


def _result(decision: str, reason: str, live_rows: list[dict], canary_rows: list[dict]) -> dict:
    return {
        "decision": decision, "reason": reason,
        "live_rate": success_rate(live_rows), "canary_rate": success_rate(canary_rows),
        "live_samples": len(live_rows), "canary_samples": len(canary_rows),
    }


def evaluate_ab(traces: list[dict], min_samples: int = 10, max_drop: float = 0.1) -> dict:
    """A/B 判定:候选相对现行版掉点超过 max_drop 即回滚,否则转正。

    - 灰度样本不足 min_samples → wait(样本太少的胜负是噪声);
    - 没有 live 对照样本 → wait(该走 evaluate_absolute);
    - 老轨迹缺 variant 字段的按 live 计(兼容 Task 13 迁移前的数据)。
    """
    live = [t for t in traces if (t.get("variant") or VARIANT_LIVE) == VARIANT_LIVE]
    canary = [t for t in traces if t.get("variant") == VARIANT_CANARY]

    if len(canary) < min_samples:
        return _result(DECISION_WAIT,
                       f"灰度样本不足({len(canary)}/{min_samples})", live, canary)

    live_rate = success_rate(live)
    if live_rate is None:
        return _result(DECISION_WAIT, "无 live 对照样本,无法 A/B 判定", live, canary)

    canary_rate = success_rate(canary)
    if canary_rate < live_rate - max_drop:
        return _result(
            DECISION_ROLLBACK,
            f"候选成功率 {canary_rate:.2f} 低于现行 {live_rate:.2f}(容差 {max_drop})",
            live, canary)
    return _result(
        DECISION_PROMOTE,
        f"候选成功率 {canary_rate:.2f} 未劣于现行 {live_rate:.2f}(容差 {max_drop})",
        live, canary)


def evaluate_absolute(traces: list[dict], min_samples: int = 30,
                      min_rate: float = 0.6) -> dict:
    """绝对值判定:没有对照组时看该 skill 自身成功率是否守住下限。"""
    rows = list(traces)
    if len(rows) < min_samples:
        return _result(DECISION_WAIT, f"样本不足({len(rows)}/{min_samples})", [], rows)

    rate = success_rate(rows)
    if rate < min_rate:
        return _result(DECISION_ROLLBACK,
                       f"成功率 {rate:.2f} 低于下限 {min_rate}", [], rows)
    return _result(DECISION_PROMOTE, f"成功率 {rate:.2f} 达标(下限 {min_rate})", [], rows)
```

- [ ] **Step 4: 运行纯函数测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_watchdog.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: 实现 skill_watchdog.py CLI**

创建 `app/scripts/skill_watchdog.py`：

```python
"""在线自进化收口:按风险档开灰度,按实战成绩自动转正 / 自动回滚。

用法:
  python -m app.scripts.skill_watchdog --start <skill-name>   按风险档开灰度/走门禁
  python -m app.scripts.skill_watchdog --start-all            对所有候选逐个执行 --start
  python -m app.scripts.skill_watchdog --check                评估活跃灰度并自动收口

分级授权(见 app/agent/skills/risk.py):
  low    改进型 + 只读工具 → 开 50% 灰度,跑赢自动转正、跑输自动回滚(全程无人);
  medium 新建 skill        → 过离线评测门禁即转正,之后按绝对成功率看门狗守着;
  high   碰钱/承诺类       → 只打印提示,**代码绝不自动转正**,必须人工执行
                             python -m app.scripts.promote_skill <name>

--check 自动转正用 force=True:灰度实战数据比离线评测是更强的证据。注意
promote() 内部的工具名校验照跑——force 从不放行编造工具名的候选。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.skills import risk as risk_mod  # noqa: E402
from app.agent.skills.validator import validate_candidate  # noqa: E402
from app.agent.skills.watchdog import (  # noqa: E402
    DECISION_PROMOTE,
    DECISION_ROLLBACK,
    evaluate_ab,
    evaluate_absolute,
)
from app.scripts.promote_skill import (  # noqa: E402
    ARCHIVE_DIR,
    CANDIDATES_DIR,
    DEFINITIONS_DIR,
    list_candidates,
    promote,
    rollback,
)


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _db():
    from app.db import get_db
    return get_db()


def start_for_candidate(skill_name: str, definitions_dir: str, candidates_dir: str,
                        archive_dir: str, db) -> dict:
    """按风险档决定该候选怎么放行。返回 {"risk","policy","action","detail"}。

    - high   → action="manual_required",不动任何东西;
    - low    → action="canary_started",登记 50% 灰度;
    - medium → action="gated"(过门禁则顺带转正) / "gate_failed"。
    """
    candidate = Path(candidates_dir) / skill_name / "SKILL.md"
    if not candidate.exists():
        return {"risk": None, "policy": None, "action": "missing",
                "detail": f"候选不存在: {candidate}"}

    content = candidate.read_text(encoding="utf-8")
    report = validate_candidate(content)
    if not report["valid"]:
        return {"risk": None, "policy": None, "action": "invalid",
                "detail": "; ".join(report["errors"])}

    is_new = not (Path(definitions_dir) / skill_name / "SKILL.md").exists()
    risk = risk_mod.classify_risk(content, is_new_skill=is_new)
    policy = risk_mod.promotion_policy(risk)

    if policy == risk_mod.POLICY_MANUAL:
        return {"risk": risk, "policy": policy, "action": "manual_required",
                "detail": "高风险(碰钱/承诺类),需人工: "
                          f"python -m app.scripts.promote_skill {skill_name}"}

    if policy == risk_mod.POLICY_CANARY_AB:
        db.start_canary(skill_name, str(candidate), risk_mod.CANARY_PERCENT, risk, policy)
        return {"risk": risk, "policy": policy, "action": "canary_started",
                "detail": f"已开 {risk_mod.CANARY_PERCENT}% 灰度,等 --check 收口"}

    # POLICY_GATE_THEN_WATCH:新建 skill 无对照组,过离线门禁即转正,之后绝对值看门狗
    from app.agent.skills.gate import default_eval_fn, gate_candidate, gate_case_ids
    from app.config.settings import settings

    case_ids = gate_case_ids(skill_name, settings.eval_dataset_path)
    gate_result = gate_candidate(
        skill_name=skill_name, candidate_path=str(candidate),
        definitions_dir=definitions_dir,
        dest_root=str(Path(archive_dir).parent / "_shadow"),
        eval_fn=default_eval_fn, case_ids=case_ids,
        tolerance=settings.skill_gate_tolerance,
    )
    if not gate_result["promote"]:
        return {"risk": risk, "policy": policy, "action": "gate_failed",
                "detail": gate_result["reason"]}

    promoted = promote(skill_name, definitions_dir, candidates_dir, archive_dir,
                       gate_result=gate_result, force=False, timestamp=_now_stamp())
    if promoted["promoted"]:
        # percent=0:已转正进正式目录,不需替换正文,只登记以便 --check 做绝对值监控
        db.start_canary(skill_name, str(candidate), 0, risk, policy)
    return {"risk": risk, "policy": policy,
            "action": "gated" if promoted["promoted"] else "promote_failed",
            "detail": promoted["reason"]}


def check_canaries(definitions_dir: str, candidates_dir: str, archive_dir: str,
                   db) -> list[dict]:
    """评估所有活跃灰度并自动收口:转正 / 回滚 / 继续观察。"""
    results: list[dict] = []
    for row in db.list_active_canaries():
        skill_name = row["skill_name"]
        traces = db.list_skill_traces(skill_name=skill_name, limit=1000)

        if row.get("policy") == risk_mod.POLICY_CANARY_AB:
            verdict = evaluate_ab(traces, min_samples=risk_mod.CANARY_MIN_SAMPLES,
                                  max_drop=risk_mod.CANARY_MAX_DROP)
        else:
            verdict = evaluate_absolute(traces, min_samples=risk_mod.ABSOLUTE_MIN_SAMPLES,
                                        min_rate=risk_mod.ABSOLUTE_MIN_RATE)

        entry = {"skill_name": skill_name, "decision": verdict["decision"],
                 "reason": verdict["reason"], "live_rate": verdict["live_rate"],
                 "canary_rate": verdict["canary_rate"],
                 "canary_samples": verdict["canary_samples"], "action": "none"}

        if verdict["decision"] == DECISION_PROMOTE:
            if row.get("policy") == risk_mod.POLICY_CANARY_AB:
                # 灰度实战胜出 → 转正(force:实战证据强于离线门禁,校验仍会跑)
                promoted = promote(skill_name, definitions_dir, candidates_dir, archive_dir,
                                   gate_result=None, force=True, timestamp=_now_stamp())
                entry["action"] = "promoted" if promoted["promoted"] else "promote_failed"
                entry["detail"] = promoted["reason"]
            else:
                entry["action"] = "watch_passed"   # 绝对值达标,结束监控
            db.finish_canary(skill_name, "promoted")

        elif verdict["decision"] == DECISION_ROLLBACK:
            if row.get("policy") == risk_mod.POLICY_CANARY_AB:
                # 候选还没进正式目录,废弃灰度即等于回滚,不必动 definitions
                entry["action"] = "canary_discarded"
            else:
                back = rollback(skill_name, definitions_dir, archive_dir)
                entry["action"] = "rolled_back" if back["rolled_back"] else "rollback_failed"
                entry["detail"] = back["reason"]
            db.finish_canary(skill_name, "rolled_back")

        results.append(entry)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill 灰度看门狗(在线自进化收口)")
    parser.add_argument("--start", metavar="SKILL", help="按风险档为该候选开灰度/走门禁")
    parser.add_argument("--start-all", action="store_true", help="对所有候选逐个执行 --start")
    parser.add_argument("--check", action="store_true", help="评估活跃灰度并自动收口")
    args = parser.parse_args()

    db = _db()

    if args.start or args.start_all:
        names = ([args.start] if args.start
                 else [c["name"] for c in list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)])
        if not names:
            print("没有候选(先跑 python -m app.scripts.synthesize_skills)")
        for name in names:
            out = start_for_candidate(name, DEFINITIONS_DIR, CANDIDATES_DIR, ARCHIVE_DIR, db)
            print(f"[{out['risk'] or '-'}/{out['action']}] {name}: {out['detail']}")

    if args.check:
        results = check_canaries(DEFINITIONS_DIR, CANDIDATES_DIR, ARCHIVE_DIR, db)
        if not results:
            print("没有活跃灰度")
        for r in results:
            rates = (f"live={r['live_rate']} canary={r['canary_rate']} "
                     f"n={r['canary_samples']}")
            print(f"[{r['decision']}/{r['action']}] {r['skill_name']}: {r['reason']} | {rates}")

    if not (args.start or args.start_all or args.check):
        parser.error("需要 --start / --start-all / --check 之一")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 运行 CLI 测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_watchdog_cli.py -v`
Expected: PASS（5 passed）

- [ ] **Step 7: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS（无新增失败）

- [ ] **Step 8: 提交**

```bash
git add app/agent/skills/watchdog.py app/scripts/skill_watchdog.py tests/test_skill_watchdog.py tests/test_skill_watchdog_cli.py
git commit -m "feat(skill): 看门狗自动转正/回滚(低危灰度跑赢自动上线,高危仍人工)"
```

---

### Task 15: 管理端补充风险档与灰度状态

**Files:**
- Modify: `app/api/app.py`（Task 11 建的 `admin_skills` 端点）
- Test: `tests/test_skill_admin_api.py`（追加用例）

**Interfaces:**
- Consumes: `classify_risk` / `promotion_policy`（Task 12）、`Database.list_active_canaries`（Task 13）
- Produces: `GET /api/admin/skills` 响应中，每个 candidate 增加 `risk` 与 `policy` 字段；顶层增加 `canaries` 列表

> 分级授权的价值在于**看得见**：哪些候选会自动上线、哪些在等你人工批、当前有几个灰度在跑、各自成绩如何。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_skill_admin_api.py` 末尾追加：

```python
# ---------- 分级授权可见性(Task 15) ----------

def test_candidates_carry_risk_and_policy():
    data = _client().get("/api/admin/skills", headers=_headers()).json()
    for item in data["candidates"]:
        assert item["risk"] in {"high", "medium", "low", None}
        assert item["policy"] in {"manual", "canary_ab", "gate_then_watch", None}


def test_active_canaries_exposed(tmp_path, monkeypatch):
    from app.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    db.start_canary("process-return", "/p/SKILL.md", 50, "low", "canary_ab")
    monkeypatch.setattr("app.db.get_db", lambda: db)

    data = _client().get("/api/admin/skills", headers=_headers()).json()

    assert len(data["canaries"]) == 1
    entry = data["canaries"][0]
    assert entry["skill_name"] == "process-return"
    assert entry["percent"] == 50
    assert entry["policy"] == "canary_ab"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_admin_api.py -v`
Expected: FAIL — `KeyError: 'canaries'`

- [ ] **Step 3: 扩展端点**

在 `app/api/app.py` 的 `admin_skills` 中，把：

```python
        try:
            candidates = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
        except Exception:  # noqa: BLE001 候选目录异常不该让总览 500
            candidates = []
```

改为：

```python
        try:
            from pathlib import Path as _Path

            from app.agent.skills.risk import classify_risk, promotion_policy

            candidates = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
            for item in candidates:
                content = _Path(item["path"]).read_text(encoding="utf-8")
                item["risk"] = classify_risk(content, is_new_skill=not item["is_improvement"])
                item["policy"] = promotion_policy(item["risk"])
        except Exception:  # noqa: BLE001 候选目录异常不该让总览 500
            candidates = []

        try:
            canaries = get_db().list_active_canaries()
        except Exception:  # noqa: BLE001
            canaries = []
```

并把 `return` 语句改为：

```python
        return {"live": live, "candidates": candidates, "traces": traces,
                "canaries": canaries}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_admin_api.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add app/api/app.py tests/test_skill_admin_api.py
git commit -m "feat(api): 管理端展示候选风险档、放行策略与活跃灰度"
```

---

## 端到端验收（全部任务完成后人工跑一遍）

- [ ] **1. 制造真实轨迹**：启动服务，在聊天里问一句会触发 skill 的话（如「我要退货，订单 ORD-20240115-001」），确认落库：

```bash
.venv/Scripts/python.exe -c "
from app.db import get_db
for r in get_db().list_skill_traces(limit=5):
    print(r['skill_name'], r['outcome'], [c['name'] for c in r['tool_calls']])
"
```
Expected: 至少一行，`skill_name` 非空。

- [ ] **2. 跑离线闭环**：

```bash
SKILL_SYNTH_ENABLED=true .venv/Scripts/python.exe -m app.scripts.synthesize_skills 50
```
Expected: 打印四类产出 + 「候选校验结论」；**候选中不应再出现编造工具名**（合成时已注入真实清单并强校验）。

- [ ] **3. 看候选**：`.venv/Scripts/python.exe -m app.scripts.promote_skill --list`
Expected: 每条候选带 `✅ 可送门禁` 或具体错误。

- [ ] **4. 走门禁转正**（会真跑 LLM 评测，约数分钟）：

```bash
.venv/Scripts/python.exe -m app.scripts.promote_skill process-return
```
Expected: 打印门禁用例、现行/候选 pass_rate 对比、promote 结论。劣化时应被拒绝且正式目录不变。

- [ ] **5. 验回滚**：

```bash
.venv/Scripts/python.exe -m app.scripts.promote_skill process-return --rollback
```
Expected: `rolled_back: True`，`definitions/process-return/SKILL.md` 恢复为转正前内容。

- [ ] **6. 管理端总览**：`curl -H "X-Admin-Token: <token>" http://127.0.0.1:8010/api/admin/skills`
Expected: 返回 live / candidates / traces / canaries 四段，每个候选带 `risk` 与 `policy`。

- [ ] **7. 分级授权判档正确**：

```bash
.venv/Scripts/python.exe -m app.scripts.skill_watchdog --start-all
```
Expected: 引用了 `apply_refund` 等写工具的候选显示 `[high/manual_required]`（**不进灰度、不动正式目录**）；只读改进型候选显示 `[low/canary_started]`。

- [ ] **8. 灰度确实分流且会话内一致**：用两个不同 `?user=` 开两个窗口各聊一轮命中该 skill 的问题，然后看轨迹的 variant 分布：

```bash
.venv/Scripts/python.exe -c "
from app.db import get_db
rows = get_db().list_skill_traces(limit=20)
for r in rows: print(r['skill_name'], r['variant'], r['outcome'], r['session_id'])
"
```
Expected: 出现 `live` 与 `canary` 两种 variant；**同一 session_id 的多行 variant 必须一致**（会话内不换版本）。

- [ ] **9. 看门狗自动收口**：`.venv/Scripts/python.exe -m app.scripts.skill_watchdog --check`
Expected: 样本不足时打印 `[wait/none]`；样本够且候选不劣化 → `[promote/promoted]` 且正式目录已被候选内容替换；候选劣化 → `[rollback/canary_discarded]` 且正式目录**未被改动**。

- [ ] **10. 高危候选不会被自动上线（安全回归，必须验）**：手工造一个引用 `apply_refund` 的候选，跑 `--start-all` 后确认 `definitions/` 无变化、`skill_canaries` 无该 skill 的活跃记录：

```bash
.venv/Scripts/python.exe -c "
from app.db import get_db
print('活跃灰度:', [c['skill_name'] for c in get_db().list_active_canaries()])
"
```
Expected: 高危 skill 名**不在**活跃灰度列表里。

---

## 完成后可以准确对外描述的能力

对齐多客「持续学习闭环」7 步后，本项目可如实声称：

| 多客步骤 | 本计划落地物 |
|---|---|
| ③ 调用 Skill 执行 | 既有 `load_skill`（G1 工作流化留待后续计划） |
| ④ 结果校验 | `SkillTurn` 按工具返回判定成败与结局 |
| ⑤ 转人工（带上下文） | 既有 HITL + 结局记为 `handoff` 进轨迹 |
| ⑥ 经验蒸馏（成功+失败） | 失败：`collect_failures_by_skill`（真实轨迹）；成功：`synthesize_from_golden`（人工接管语料） |
| ⑦ Skill 沉淀/自我进化 | 合成候选 → 工具名强校验 → 风险自动判档 → 低危灰度 A/B 自动转正 / 劣化自动回滚，高危人工确认 |

对齐多客材料里那句边界（「安全机制限制高风险流程大规模自主变更，重大业务流程变更仍需要人工确认」）：本项目的高风险档判定是**自动**的（按引用工具与承诺措辞，正文与 `workflow` 声明两条通道都算），且高危档在**开灰度时**与**真正转正前**各判一次——两处都会拒绝自动上线（转正前那次是必需的：候选文件可能在灰度期间被 `improve_skill` 原地覆盖成动钱内容）。

**可以如实说的**：
- 「Skill 自进化闭环：执行轨迹驱动的经验蒸馏 + 工具契约校验 + 灰度 A/B + 看门狗自动回滚」
- 「分级授权：低风险自我迭代无人值守，碰钱与承诺类流程强制人工确认」
- 「候选转正前必过静态校验与评测门禁，转正自动备份、原子写入，支持一键回滚」

**不能说的**（会经不起追问）：
- 「全自动无人值守进化」——高危档是刻意锁人工的，这是设计而非缺陷，讲清楚反而是加分项。
- 「在线强化学习 / 模型自训练」——本方案进化的是 **Skill 指令**，不改模型权重。
- 「实时进化」——蒸馏是离线批处理（`synthesize_skills`），只有**放行**是在线自动的。

## 已知边界（必须一并说明，不可回避）

1. **「成功率」不等于「答得对」**。门禁与看门狗判定用的成功率，衡量的是「是否转人工 / 工具是否报错」，**不含答案正确性**。一个工具调用干净但内容答错的回复会被计为成功（1.0）。要覆盖正确性需依赖评测集里的 judge 维度，而门禁只跑该 skill 打标的子集。
2. **评测门禁的证据很薄**。目前 `cases.json` 里每个 skill 只打了 **1 条**用例，所以「无劣化」实际上只由一条用例的通过/失败决定。更关键的是：**新建 skill 没有任何打标用例**，`gate_case_ids` 返回空 → 门禁 fail-closed → `medium` 档（新建 skill）实际上**永远无法通过门禁**。要让 medium 档真正可用，必须先为新场景补打标用例。
3. **轨迹有生存者偏差**。执行轨迹只在 `chat()` 正常返回时落库；若本轮在 ReAct、出话流水线或结构化提取中抛异常，该轮不会被记录。而「坏指令正文」最可能造成的恰恰是这类异常（如最终 JSON 畸形）——也就是说 A/B 判定看不到候选造成的这部分失败。
4. **灰度只换指令正文，不换 `workflow` 声明**。改了声明（guards/slots）的候选，其声明在灰度期间**从未被实际执行**（强制与约束文案都用 live 版），转正后才首次生效。这类候选若只涉及只读 skill 会被判低危并走自动灰度，风险分级兜不住这一条。
5. **默认开关不对称**：灰度路由 `SKILL_CANARY_ENABLED` 默认**开**，而离线合成 `SKILL_SYNTH_ENABLED` 默认**关**。即默认状态下不会产生新候选，但一旦有人登记了灰度，分流就是生效的。描述安全姿态时应明确这一点。

## 后续计划（不在本计划内）

- **G1 Skill 工作流化**：`load_skill` 返回结构化步骤（前置校验 → 工具序列 → 分支 → 收口），并填上 `app/agent/strategies/` 里 Plan-Execute / Reflexion / REWOO 的空实现。依赖本计划的 `skill_traces` 做分支覆盖率统计。
- **前端自进化面板**：把 `/api/admin/skills` 渲染进工作台（现行技能 / 待审候选 / 成功率），一键触发门禁。
