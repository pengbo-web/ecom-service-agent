# 长期记忆三层写入(save_user_memory 即时写工具) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 新增 `save_user_memory` 工具——Agent 在 ReAct 循环内**即时**把用户显式表达的偏好/身份/重要事实写入长期记忆(FTS 同步索引,跨会话立即生效),补齐生产语义的三层写入:**显式即时写(新)/ 隐式会话末提取(现有 consolidate)/ 策展治理(现有 curation)**。

**Architecture:** 写路径与读路径同构:工具放 `memory_tool.py`(与 `recall_user_memory` 共用每轮刷新的 ContextVar manager,天然继承串户防护);写入走 `LongTermMemory.add_facts + save`(自带内容去重、max_facts 裁剪、FTS 全量重同步);注册进 `_TOOL_MAP` + 工具定义 + 三域公共工具集;三域提示词"工具使用原则"各加一条使用与**负面**约束。零新依赖、零新开关(随 `memory_enabled` 走)。

**Tech Stack:** 现有 memory/tools 体系,Python 标准库。

## Global Constraints

- **共用读路径的 manager 通道**:工具内用 `memory_tool._current_manager()` 取当轮 manager(**不得**新建全局);manager 为 None 或 `memory_enabled=False` → 返回 `{"success": False, "error": "记忆系统未启用"}`,**绝不抛**。
- **category 白名单(逐字)**:`identity / preference / behavior / issue / other`(与 `MemoryFact` 注释一致);不在白名单 → 归 `other`,不报错。
- **content 校验**:strip 后空 → `{"success": False, "error": "记忆内容不能为空"}`;超 200 字截断到 200(防模型灌长文)。
- **去重语义**:复用 `add_facts` 的按内容(lower)去重;重复写返回 `{"success": True, "already_known": true, ...}`(前后 facts 数不变即判重复)。
- **source_session**:从 `app.agent.tools.bargain` 的当前会话 ContextVar 取(读它的公开 getter,没有公开 getter 就 import 模块级 `_current_session_id.get()`——以实际代码为准,报告说明);取不到用 `""`。
- **写后立即持久化**:`manager.ltm.save()`(同时触发 FTS 全量重同步)——工具返回时记忆已跨会话可用。
- **提示词负面约束必须有**:"闲聊、猜测或未经确认的信息不要记录"。
- **不改** consolidate/curation/recall 任何现有逻辑(三层各司其职);工具**不是**风险动作(不进 RISK_ACTIONS,不需要 consent)。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 `origin feature/w1-service-streaming`,绝不 upstream。
- `.venv/Scripts/python.exe`;测试不 print emoji。

## File Structure

| 文件 | 责任 |
|---|---|
| `app/agent/tools/memory_tool.py`(改) | `save_user_memory(content, category="other") -> dict` |
| `app/agent/tools/registry.py`(改) | `_TOOL_MAP` 注册 + 工具 JSON 定义 |
| `app/multi_agent/agents.py`(改) | `_COMMON_TOOLS` 加 `save_user_memory`(三域共用) |
| `app/prompts/agents.py`(改) | 三域"工具使用原则"各加一条(含负面约束) |
| `tests/test_memory_write_tool.py`(新) | 全部行为测试 |

---

### Task M1: save_user_memory 工具 + 注册 + 提示词

**Files:**
- Modify: `app/agent/tools/memory_tool.py`、`app/agent/tools/registry.py`、`app/multi_agent/agents.py`、`app/prompts/agents.py`
- Test: `tests/test_memory_write_tool.py`(新)

**Interfaces:**
- Consumes: `memory_tool._current_manager()`/`set_memory_manager`;`LongTermMemory.add_facts/save/recall`;`MemoryFact(content, category, created_at, source_session)`。
- Produces: `save_user_memory(content: str, category: str = "other") -> dict`;registry 定义名 `save_user_memory`;`_COMMON_TOOLS` 含之(三域画像自动获得)。

- [ ] **Step 1: 写失败测试** `tests/test_memory_write_tool.py`

```python
"""save_user_memory:显式记忆即时写。全离线(不触网——add_facts/save 纯本地)。"""

from datetime import datetime

from app.agent.memory.manager import MemoryManager
from app.agent.tools.memory_tool import recall_user_memory, save_user_memory, set_memory_manager
from app.agent.tools.registry import TOOL_DEFINITIONS, execute_tool
from app.multi_agent.agents import AGENT_CONFIGS
from app.prompts.agents import AFTERSALE_PROMPT, MIDSALE_PROMPT, PRESALE_PROMPT


def _manager(tmp_path, user_id="u1"):
    m = MemoryManager(client=None, model="test", user_id=user_id,
                      memory_dir=str(tmp_path / "mem"), memory_enabled=True)
    set_memory_manager(m)
    return m


def test_save_persists_and_is_immediately_recallable(tmp_path):
    m = _manager(tmp_path)
    r = save_user_memory("用户偏好红色衣服", category="preference")
    assert r["success"] is True and r.get("already_known") is not True
    # 即时可召回(工具读路径)
    recalled = recall_user_memory()
    assert any("红色衣服" in f["content"] for f in recalled["long_term_facts"])
    # 已持久化:新 manager 重载还在(跨会话生效的本质)
    m2 = MemoryManager(client=None, model="test", user_id="u1",
                       memory_dir=str(tmp_path / "mem"), memory_enabled=True)
    assert any("红色衣服" in f.content for f in m2.ltm.facts)
    # FTS 同步:关键词召回命中
    assert any("红色衣服" in c for c in m2.ltm.recall("红色"))


def test_duplicate_content_reports_already_known(tmp_path):
    _manager(tmp_path)
    save_user_memory("用户是钻石会员", category="identity")
    r = save_user_memory("用户是钻石会员", category="identity")
    assert r["success"] is True and r["already_known"] is True


def test_invalid_category_falls_back_to_other(tmp_path):
    m = _manager(tmp_path)
    save_user_memory("用户常在深夜下单", category="不存在的类别")
    assert m.ltm.facts[-1].category == "other"


def test_empty_content_rejected(tmp_path):
    _manager(tmp_path)
    r = save_user_memory("   ")
    assert r["success"] is False


def test_long_content_truncated_to_200(tmp_path):
    m = _manager(tmp_path)
    save_user_memory("长" * 300, category="other")
    assert len(m.ltm.facts[-1].content) == 200


def test_no_manager_returns_error_not_raise():
    set_memory_manager(None)
    r = save_user_memory("x")
    assert r["success"] is False


def test_registered_in_registry_and_common_tools(tmp_path):
    names = {d["function"]["name"] for d in TOOL_DEFINITIONS}
    assert "save_user_memory" in names
    for cfg in AGENT_CONFIGS.values():
        assert "save_user_memory" in cfg["tools"]      # 三域公共工具
    _manager(tmp_path)
    out = execute_tool("save_user_memory", {"content": "用户偏好蓝色", "category": "preference"})
    assert '"success": true' in out.lower() or "true" in out.lower()


def test_prompts_carry_usage_and_negative_guidance():
    for p in (PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT):
        assert "save_user_memory" in p
        assert "不要记录" in p          # 负面约束必须在
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_memory_write_tool.py -q` FAIL

- [ ] **Step 3: 实现 memory_tool.py**(加在 recall_user_memory 之后)

```python
_VALID_CATEGORIES = {"identity", "preference", "behavior", "issue", "other"}


def save_user_memory(content: str = "", category: str = "other") -> dict:
    """把用户明确表达的偏好/身份/重要事实即时写入长期记忆(跨会话立即生效)。

    三层写入的"显式即时写"层:与会话末 consolidate(隐式提取)、curation
    (策展治理)并存互补。写入即 save——FTS 同步索引,下一轮即可召回。
    """
    manager = _current_manager()
    if manager is None or not manager.memory_enabled:
        return {"success": False, "error": "记忆系统未启用"}
    content = (content or "").strip()
    if not content:
        return {"success": False, "error": "记忆内容不能为空"}
    content = content[:200]
    if category not in _VALID_CATEGORIES:
        category = "other"

    try:
        from app.agent.tools.bargain import _current_session_id
        source = _current_session_id.get() or ""
    except Exception:
        source = ""

    from datetime import datetime
    from app.agent.memory.long_term import MemoryFact
    before = len(manager.ltm.facts)
    manager.ltm.add_facts([MemoryFact(
        content=content, category=category,
        created_at=datetime.now().isoformat(timespec="seconds"),
        source_session=source,
    )])
    already = len(manager.ltm.facts) == before
    manager.ltm.save()          # 立即持久化 + FTS 重同步
    return {"success": True, "saved": content, "category": category,
            "already_known": already, "total_facts": len(manager.ltm.facts)}
```

(bargain 若有公开 getter 就用公开的,没有再用 `_current_session_id.get()`——以实际代码为准。)

- [ ] **Step 4: registry.py**——`_TOOL_MAP` 加 `"save_user_memory": save_user_memory`(import 同行补);`TOOL_DEFINITIONS` 在 recall_user_memory 定义后插:

```python
    {
        "type": "function",
        "function": {
            "name": "save_user_memory",
            "description": (
                "把用户明确表达的个人偏好、身份信息或重要事实即时写入长期记忆"
                "（跨会话永久生效）。仅当用户清晰说出关于自己的事实时使用，"
                "例如「我喜欢红色」「我对海鲜过敏」「以后都发顺丰」。"
                "闲聊内容、你的猜测、未经用户确认的信息不要写入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的事实，用第三人称简洁陈述，如「用户偏好红色衣服」",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["identity", "preference", "behavior", "issue", "other"],
                        "description": "事实类别：identity=身份/会员，preference=偏好，behavior=行为习惯，issue=问题记录，other=其他",
                    },
                },
                "required": ["content"],
            },
        },
    },
```

- [ ] **Step 5: agents.py + prompts**——`_COMMON_TOOLS` 加 `"save_user_memory"`;三域提示词"工具使用原则"末尾各加同一条:

```
N. 当顾客明确表达个人偏好、身份信息或重要事实（如「我喜欢红色」「我对海鲜过敏」）时，调用 save_user_memory 即时记录（用第三人称简述）；闲聊、猜测或未经顾客确认的信息不要记录
```

(N = 各段现有编号顺延;三段措辞一致。)

- [ ] **Step 6: 跑通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_write_tool.py tests/test_memory.py::test_stm_reset tests/test_memory_tool_isolation.py tests/test_orchestrator_unified.py tests/test_controller_agent.py tests/test_react_degrade.py -q`
Expected: PASS(orchestrator 的工具子集断言若列了精确集合需同步——**允许**的适配)

- [ ] **Step 7: 提交** `feat(memory): M1 save_user_memory 即时写工具——显式记忆三层写入`

---

### Task M2: 端到端冒烟(控制方执行)

- [ ] 重启服务 → 登录任意用户 → 发"我特别喜欢红色的衣服,记住哦"
- [ ] 活动面板出现 `execute_tool save_user_memory`(args 含第三人称事实)
- [ ] **不点巩固**,直接查「记忆」Tab(或 /api/memory 带 token)→ 事实已在 ✅(即时生效的核心验收)
- [ ] 新开会话(结束翻篇)问"我喜欢什么颜色的衣服" → 能答红色(跨会话即时可用)
- [ ] 负面对照:闲聊一句("今天天气不错")→ 不触发该工具
- [ ] Langfuse:该轮 trace 里 TOOL 节点 `execute_tool save_user_memory` 可见
- [ ] `.superpowers/sdd/progress.md` 记账

## 总量与顺序

M1(~0.4d)→ M2(~0.1d),共 **~0.5 人日**。

## Self-Review

- **覆盖核对**:即时写+FTS 同步+跨会话生效(M1 Step1 首测三段断言)✅;三域提示词含负面约束 ✅;去重/截断/坏类别/无 manager 四个边界 ✅;三层并存不改现有 consolidate/curation ✅;冒烟含负面对照与 Langfuse 可见性 ✅。
- **占位符扫描**:无 TBD;bargain getter 的"以实际代码为准"给了两个具体选项,非开放性。
- **类型一致性**:`save_user_memory(content, category="other") -> dict` 与 registry 定义 required=["content"]、enum 五类一致;`_COMMON_TOOLS` 集合语法与现有一致。
- **已知取舍**:不做"删除/修改记忆"工具(YAGNI,策展已负责治理);200 字截断而非拒绝(工具容错优先)。
