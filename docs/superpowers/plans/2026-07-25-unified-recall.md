# 统一召回层(Unified Recall) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对齐阿里小蜜/京东言犀的"存储分离、召回统一"模式——一个 Recall 接口挂 N 个召回源(用户 profile / 长期记忆 / 短期记忆 / 平台知识库),每轮回答前统一预检索注入上下文,KB 知识不再依赖模型自觉调工具(实测触发率 5/127,是政策编造的空档)。

**Architecture:** 新增 `app/agent/recall/` 模块:`kb.py` 是平台知识预召回源(复用现有 `search_knowledge`,加阈值/预算/容错),`service.py` 是统一召回入口(按序调度 profile/LTM/STM/KB 四源,单源失败隔离)。`EcomAgent._build_messages` 改为调用召回服务(每轮缓存,react 多步不重复 embedding),并发射 `recall` 事件供前端思考过程面板与 tracer 观测。存储不动:用户记忆(一人一份可写)与知识库(全局只读)保持物理分离,只统一召回接口。

**Tech Stack:** Python 3.11 + pytest;前端 React + vitest + testing-library。不新增依赖。

## Global Constraints

- Python 一律用 `.venv/Scripts/python.exe`(不是 conda base);pytest 命令:`.venv/Scripts/python.exe -m pytest <file> -v`
- Windows gbk 控制台:测试/脚本内不要 `print` emoji 或依赖控制台中文输出做断言
- 提交信息末尾:`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;只推 `origin feature/w1-service-streaming`,严禁推 upstream;`.env` 严禁提交
- 存储红线:**不得**把 KB 数据写入用户记忆库,不得改动 KB / 记忆的任何存储结构——本次只加"召回"层
- 容错红线:预召回是增强不是依赖——任何源失败(索引缺失/embedding 超时/profile 库坏)只丢该源,绝不抛出打断回复主流程
- 现有 `MemoryManager.build_memory_prompt_sections` **保留不改**(既有测试引用它);新召回服务与它并存,`chat.py` 换用新服务
- 前端 SSE 事件类型是开放 record(`SSEEvent = Record<string, any> & { type: string }`),后端事件全量透传,新事件类型无需改 sse.ts
- 工作目录:`D:\2026项目\ecom-service-agent`(git 仓库根);前端在 `webui/` 子目录,vitest 命令:`cd webui && npx vitest run <file>`

---

### Task 1: KB 预召回源 + 配置

**Files:**
- Create: `app/agent/recall/__init__.py`(空文件)
- Create: `app/agent/recall/kb.py`
- Modify: `app/config/settings.py`(在 `grounding_total_max_chars`(155 行附近)之后插入新配置块)
- Test: `tests/test_recall_kb.py`

**Interfaces:**
- Consumes: `app.agent.tools.knowledge.search_knowledge(query, top_k) -> dict`(已存在,返回 `{"success": bool, "results": [{"doc","section","score","text"}]}`)
- Produces: `kb_recall(query: str | None) -> KbRecall`,其中 `KbRecall(section: str | None, hits: list[dict])`;`hits` 元素为 `{"doc": str, "section": str, "score": float}`。Task 2 的 service 与 Task 3 的事件都消费这两个字段。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recall_kb.py`:

```python
"""KB 预召回源:阈值过滤/字符预算/门控/容错。"""

import app.agent.recall.kb as kb_mod
from app.agent.recall.kb import kb_recall
from app.config.settings import settings


def _fake_results(*rows):
    return {"success": True, "backend": "numpy", "query": "q",
            "results": [{"doc": d, "section": s, "score": sc, "text": t}
                        for d, s, sc, t in rows]}


def test_hit_builds_section_and_hits(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天无理由", 0.62, "签收7天内可退")))
    r = kb_recall("退货政策是什么")
    assert r.section is not None
    assert "签收7天内可退" in r.section
    assert "退换货政策/七天无理由" in r.section
    assert "平台知识" in r.section          # 注入段有明确标头,提示词规则按此标头引用
    assert r.hits == [{"doc": "退换货政策", "section": "七天无理由", "score": 0.62}]


def test_below_threshold_filtered(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("FAQ", "无关", 0.10, "无关内容")))
    r = kb_recall("退货政策是什么")
    assert r.section is None and r.hits == []


def test_disabled_gate(monkeypatch):
    monkeypatch.setattr(settings, "recall_kb_enabled", False)
    called = []
    monkeypatch.setattr(kb_mod, "search_knowledge", lambda q, top_k: called.append(q))
    assert kb_recall("退货政策是什么").section is None
    assert called == []                     # 关开关连检索都不发起


def test_short_or_empty_query_skipped(monkeypatch):
    called = []
    monkeypatch.setattr(kb_mod, "search_knowledge", lambda q, top_k: called.append(q))
    assert kb_recall("嗯").section is None      # 短于 recall_kb_min_query_chars
    assert kb_recall(None).section is None
    assert kb_recall("   ").section is None
    assert called == []


def test_search_error_returns_empty(monkeypatch):
    def boom(q, top_k):
        raise RuntimeError("embedding down")
    monkeypatch.setattr(kb_mod, "search_knowledge", boom)
    r = kb_recall("退货政策是什么")
    assert r.section is None and r.hits == []   # 容错:失败不抛出


def test_search_failure_dict_returns_empty(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: {"success": False, "error": "索引缺失", "results": []})
    assert kb_recall("退货政策是什么").section is None


def test_char_budget_drops_tail(monkeypatch):
    long_text = "长" * 1200
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("政策", "A", 0.9, "短片段"),
                                                       ("政策", "B", 0.8, long_text)))
    r = kb_recall("退货政策是什么")
    assert "短片段" in r.section and long_text not in r.section
    assert [h["section"] for h in r.hits] == ["A"]   # 超预算片段连 hits 也不记
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_kb.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'app.agent.recall'`

- [ ] **Step 3: 加配置**

在 `app/config/settings.py` 的 `grounding_total_max_chars: int = 8000` 行之后插入:

```python
    # 统一召回层(Unified Recall):存储分离、召回统一——每轮预检索注入,
    # 政策类知识不再依赖模型自觉调 search_knowledge(实测触发率低,是编造空档)
    recall_kb_enabled: bool = True        # KB 预召回开关;关=回到纯工具式
    recall_kb_top_k: int = 2              # 每轮最多注入的 KB 片段数
    recall_kb_min_score: float = 0.30     # 相似度阈值,低于不注入(防无关知识污染上下文)
    recall_kb_max_chars: int = 1200       # 注入段字符预算,超出丢弃后续片段
    recall_kb_min_query_chars: int = 4    # 问题太短(如"嗯")不触发,省一次 embedding
```

- [ ] **Step 4: 写实现**

创建 `app/agent/recall/__init__.py`(空)与 `app/agent/recall/kb.py`:

```python
"""KB 预召回源:统一召回层的"平台知识"通道。

存储分离、召回统一(对齐阿里小蜜/京东言犀模式):知识库仍是全局共享只读存储,
本模块只负责在每轮回答前**主动**检索并格式化为可注入的 system prompt 段,
不再依赖模型自觉调用 search_knowledge 工具(实测触发率低,是政策编造的空档)。

容错原则:预召回是增强,不是依赖——任何失败(索引缺失/embedding 超时)都
返回空结果,绝不阻塞回复主流程。
"""

from dataclasses import dataclass, field

from app.config.settings import settings
from app.agent.tools.knowledge import search_knowledge


@dataclass
class KbRecall:
    section: str | None = None                       # 可注入的 system prompt 段;无命中为 None
    hits: list[dict] = field(default_factory=list)   # [{"doc","section","score"}] 供事件/观测展示


_HEADER = (
    "【平台知识(自动检索)】以下片段来自平台官方知识库,按与本轮用户问题的相关度自动检索;"
    "与当前问题无关时忽略。回答政策/规则问题时优先引用以下内容,不得与之矛盾:"
)


def kb_recall(query: str | None) -> KbRecall:
    """对本轮用户问题做 KB 预检索,返回格式化注入段与命中明细。"""
    if not settings.recall_kb_enabled:
        return KbRecall()
    if not query or len(query.strip()) < settings.recall_kb_min_query_chars:
        return KbRecall()
    try:
        result = search_knowledge(query, top_k=settings.recall_kb_top_k)
    except Exception:
        return KbRecall()
    if not result.get("success"):
        return KbRecall()

    lines: list[str] = []
    hits: list[dict] = []
    used = len(_HEADER)
    for r in result.get("results", []):
        if r.get("score", 0.0) < settings.recall_kb_min_score:
            continue
        line = f"- [{r.get('doc', '')}/{r.get('section', '')}] {r.get('text', '')}"
        if used + len(line) > settings.recall_kb_max_chars:
            break
        lines.append(line)
        used += len(line)
        hits.append({"doc": r.get("doc", ""), "section": r.get("section", ""),
                     "score": r.get("score", 0.0)})
    if not lines:
        return KbRecall()
    return KbRecall(section=_HEADER + "\n" + "\n".join(lines), hits=hits)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_kb.py -v`
Expected: 7 passed

- [ ] **Step 6: 提交**

```bash
git add app/agent/recall/__init__.py app/agent/recall/kb.py app/config/settings.py tests/test_recall_kb.py
git commit -m "feat: KB 预召回源(统一召回层第一源,阈值/预算/容错)"
```

---

### Task 2: 统一召回服务(RecallService)

**Files:**
- Create: `app/agent/recall/service.py`
- Test: `tests/test_recall_service.py`

**Interfaces:**
- Consumes: Task 1 的 `kb_recall(query) -> KbRecall`;`MemoryManager` 实例的既有属性:`memory_enabled: bool`、`ltm.user_id: str`、`ltm.build_prompt_section(query) -> str`、`stm.build_prompt_section() -> str`;`app.agent.memory.profile.get_profile_store()`(可能返回 None)
- Produces: `build_recall_sections(memory_manager, query: str | None) -> RecallResult`,其中 `RecallResult(sections: list[dict], kb_hits: list[dict])`;`sections` 元素为 `{"role": "system", "content": str}`。Task 3 的 chat 装配消费它。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recall_service.py`:

```python
"""统一召回服务:多源合并/顺序/单源失败隔离/memory 开关独立性。"""

from types import SimpleNamespace

import app.agent.recall.service as svc
from app.agent.recall.kb import KbRecall
from app.config.settings import settings


def _mm(memory_enabled=True, ltm_text="记忆事实", stm_text="短期摘要", ltm_raises=False):
    def ltm_section(query):
        if ltm_raises:
            raise RuntimeError("fts broken")
        return ltm_text
    ltm = SimpleNamespace(user_id="u1", build_prompt_section=ltm_section)
    stm = SimpleNamespace(build_prompt_section=lambda: stm_text)
    return SimpleNamespace(memory_enabled=memory_enabled, ltm=ltm, stm=stm)


def test_merges_memory_and_kb_in_order(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall",
                        lambda q: KbRecall(section="【平台知识(自动检索)】KB段",
                                           hits=[{"doc": "d", "section": "s", "score": 0.5}]))
    r = svc.build_recall_sections(_mm(), "退货政策")
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要", "【平台知识(自动检索)】KB段"]
    assert all(x["role"] == "system" for x in r.sections)
    assert r.kb_hits == [{"doc": "d", "section": "s", "score": 0.5}]


def test_memory_off_kb_still_works(monkeypatch):
    """存储分离的意义:平台知识召回不受用户记忆开关影响。"""
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall(section="KB段", hits=[]))
    r = svc.build_recall_sections(_mm(memory_enabled=False), "退货政策")
    assert [x["content"] for x in r.sections] == ["KB段"]


def test_memory_source_error_isolated(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall())
    r = svc.build_recall_sections(_mm(ltm_raises=True), "q")
    assert [x["content"] for x in r.sections] == ["短期摘要"]   # LTM 坏了只丢 LTM


def test_kb_error_isolated(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    def boom(q):
        raise RuntimeError("kb down")
    monkeypatch.setattr(svc, "kb_recall", boom)
    r = svc.build_recall_sections(_mm(), "q")
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要"]
    assert r.kb_hits == []


def test_empty_sources_yield_empty(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall())
    r = svc.build_recall_sections(_mm(ltm_text="", stm_text=""), "q")
    assert r.sections == [] and r.kb_hits == []


def test_none_memory_manager_kb_only(monkeypatch):
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall(section="KB段", hits=[]))
    r = svc.build_recall_sections(None, "q")
    assert [x["content"] for x in r.sections] == ["KB段"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_service.py -v`
Expected: FAIL,`ModuleNotFoundError`(service 未创建)

- [ ] **Step 3: 写实现**

创建 `app/agent/recall/service.py`:

```python
"""统一召回服务(Recall Service):一个入口挂 N 个召回源。

生产对齐(阿里小蜜/京东言犀):**存储分离、召回统一**——
- 用户记忆(一人一份、可写、隐私)与平台知识库(全局共享、只读)存储各自独立;
- 回答前由本服务统一调度各源检索,合并为 system prompt 注入段。

源列表(注入顺序即代码顺序):
  profile     结构化用户档案(memory_profile_enabled 门控)
  long_term   长期记忆事实(FTS 召回命中优先)
  short_term  短期记忆滚动摘要
  kb          平台知识预检索(recall_kb_enabled 门控,独立于 memory_enabled)

隔离原则:任一源抛错只丢该源,不影响其它源与主流程。
"""

from dataclasses import dataclass, field

from app.config.settings import settings
from app.agent.recall.kb import KbRecall, kb_recall


@dataclass
class RecallResult:
    sections: list[dict] = field(default_factory=list)   # [{"role":"system","content":...}]
    kb_hits: list[dict] = field(default_factory=list)    # KB 命中明细(供 recall 事件/观测)


def _profile_section(memory_manager, query):
    if not settings.memory_profile_enabled:
        return None
    from app.agent.memory.profile import get_profile_store
    store = get_profile_store()
    if store is None:
        return None
    return store.get(memory_manager.ltm.user_id).to_prompt() or None


def _long_term_section(memory_manager, query):
    return memory_manager.ltm.build_prompt_section(query) or None


def _short_term_section(memory_manager, query):
    return memory_manager.stm.build_prompt_section() or None


def build_recall_sections(memory_manager, query: str | None) -> RecallResult:
    """统一召回入口:按源顺序检索,合并为注入段列表;单源失败隔离。"""
    result = RecallResult()
    memory_on = memory_manager is not None and getattr(memory_manager, "memory_enabled", False)
    if memory_on:
        for source in (_profile_section, _long_term_section, _short_term_section):
            try:
                text = source(memory_manager, query)
            except Exception:
                continue
            if text:
                result.sections.append({"role": "system", "content": text})
    try:
        kb = kb_recall(query)
    except Exception:
        kb = KbRecall()
    if kb.section:
        result.sections.append({"role": "system", "content": kb.section})
        result.kb_hits = kb.hits
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_service.py tests/test_recall_kb.py -v`
Expected: 13 passed

- [ ] **Step 5: 提交**

```bash
git add app/agent/recall/service.py tests/test_recall_service.py
git commit -m "feat: 统一召回服务——一个 Recall 接口挂四源,单源失败隔离"
```

---

### Task 3: 接线 chat.py(每轮缓存 + recall 事件)与 tracer 观测

**Files:**
- Modify: `app/agent/chat.py`(`__init__` 约 41 行、`chat()` 约 126 行、`_build_messages` 395-417 行)
- Modify: `app/observability/tracer.py`(`on_event` 的 `elif etype == "guard":` 分支之后)
- Test: `tests/test_recall_wiring.py`

**Interfaces:**
- Consumes: Task 2 的 `build_recall_sections(memory_manager, query) -> RecallResult`
- Produces: SSE 事件 `{"type": "recall", "source": "kb", "hits": [{"doc","section","score"}]}`(每轮至多一次,仅 KB 有命中时);tracer 落 `kind="recall"` 的 span,`name="recall:kb"`,`meta={"hits": [...]}`。Task 4 前端消费该事件。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recall_wiring.py`:

```python
"""chat 装配接线:召回段注入/每轮只检索一次/recall 事件只发一次/tracer 落 span。"""

import itertools

from app.agent.chat import EcomAgent
from app.agent.recall.service import RecallResult
from app.observability.store import TraceStore
from app.observability.tracer import Tracer


def _agent():
    # 构造不触发网络调用(与 tests/test_emit.py 同模式)
    return EcomAgent(session_path="app/sessions/_test_recall_wiring.json")


def _fake_recall(calls):
    def fake(mm, query):
        calls.append(query)
        return RecallResult(
            sections=[{"role": "system", "content": "【平台知识(自动检索)】测试片段"}],
            kb_hits=[{"doc": "退换货政策", "section": "七天无理由", "score": 0.9}],
        )
    return fake


def test_build_messages_injects_and_caches_per_turn(monkeypatch):
    calls = []
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", _fake_recall(calls))
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None

    m1 = agent._build_messages()
    m2 = agent._build_messages()      # 模拟 react 第二步:必须复用缓存

    assert any(msg["role"] == "system" and "测试片段" in msg["content"] for msg in m1)
    assert any(msg["role"] == "system" and "测试片段" in msg["content"] for msg in m2)
    assert calls == ["退货政策是什么"]                       # 整轮只检索一次
    recall_events = [e for e in events if e["type"] == "recall"]
    assert len(recall_events) == 1                           # 事件也只发一次
    assert recall_events[0]["source"] == "kb"
    assert recall_events[0]["hits"][0]["doc"] == "退换货政策"


def test_new_user_turn_recomputes(monkeypatch):
    calls = []
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", _fake_recall(calls))
    agent = _agent()
    agent.event_sink = lambda e: None
    agent.raw_messages.append({"role": "user", "content": "第一问"})
    agent._turn_recall = None
    agent._build_messages()
    agent.raw_messages.append({"role": "user", "content": "第二问"})
    agent._build_messages()
    assert calls == ["第一问", "第二问"]                      # last_user 变了要重算


def test_no_hits_no_event(monkeypatch):
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q: RecallResult())
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "你好"})
    agent._turn_recall = None
    agent._build_messages()
    assert [e for e in events if e["type"] == "recall"] == []


def test_tracer_records_recall_span(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count(start=0, step=1)
    ids = itertools.count(start=1)
    tracer = Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}")
    with tracer.start_trace("sess1", "退货政策") as t:
        tracer.on_event({"type": "recall", "source": "kb",
                         "hits": [{"doc": "退换货政策", "section": "七天无理由", "score": 0.62}]})
    saved = store.get_trace(t.trace_id)
    recall_spans = [s for s in saved["spans"] if s["kind"] == "recall"]
    assert len(recall_spans) == 1
    assert recall_spans[0]["name"] == "recall:kb"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_wiring.py -v`
Expected: FAIL(`_turn_recall` 属性不存在 / 注入段缺失 / recall span 数为 0)

- [ ] **Step 3: 改 chat.py**

3a. `__init__`(在 `self.system_prompt = SYSTEM_PROMPT` 行之后,约 41 行)加:

```python
        self._turn_recall = None   # (last_user, RecallResult) 每轮预召回缓存:react 多步共享,不重复 embedding
```

3b. `chat()` 里 `self._step_seq = 0`(约 126 行)之后加:

```python
        self._turn_recall = None   # 新一轮:召回缓存作废,按本轮问题重检索
```

3c. `_build_messages` 中把这一行:

```python
        messages.extend(self.memory_manager.build_memory_prompt_sections(query=last_user))
```

替换为:

```python
        # 统一召回层:profile/LTM/STM/KB 四源一次装配(存储分离、召回统一)。
        # 每轮缓存:react 循环内多次组消息不重复检索(KB 预检索有 embedding 开销)。
        from app.agent.recall.service import build_recall_sections
        if self._turn_recall is None or self._turn_recall[0] != last_user:
            rr = build_recall_sections(self.memory_manager, last_user)
            self._turn_recall = (last_user, rr)
            if rr.kb_hits:   # 首次计算且 KB 有命中才发事件(前端思考面板+tracer 各消费一次)
                self._emit({"type": "recall", "source": "kb", "hits": rr.kb_hits})
        messages.extend(self._turn_recall[1].sections)
```

- [ ] **Step 4: 改 tracer.py**

`on_event` 中 `elif etype == "guard":` 分支的闭合之后(与 guard 平级)追加:

```python
        elif etype == "recall":
            now = self._now()
            trace.spans.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"recall:{event.get('source')}", kind="recall",
                started_at=now, ended_at=now, latency_ms=0.0,
                meta={"hits": event.get("hits", [])},
            ))
```

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_wiring.py -v`
Expected: 4 passed

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q`
Expected: 全部通过。若有既有测试因 `_build_messages` 不再调 `build_memory_prompt_sections` 而失败(如 monkeypatch 该方法的测试),把该测试的 patch 目标改为 `app.agent.recall.service.build_recall_sections` 并保持断言语义不变——注入段内容本身没变,只有装配入口变了。

- [ ] **Step 6: 提交**

```bash
git add app/agent/chat.py app/observability/tracer.py tests/test_recall_wiring.py
git commit -m "feat: chat 装配接入统一召回层(每轮缓存+recall 事件+tracer span)"
```

---

### Task 4: 前端 recall 事件展示

**Files:**
- Modify: `webui/src/components/ChatView.tsx`(事件白名单一行,约 84 行)
- Modify: `webui/src/components/AgentActivity.tsx`(新增 recall 渲染分支 + BookOpen 图标 import)
- Test: `webui/src/tests/chat-components.test.tsx`(追加用例)

**Interfaces:**
- Consumes: Task 3 的 SSE 事件 `{"type": "recall", "source": "kb", "hits": [{"doc","section","score"}]}`
- Produces: 思考过程面板一行「预召回 → 平台知识:退换货政策/七天无理由」

- [ ] **Step 1: 写失败测试**

在 `webui/src/tests/chat-components.test.tsx` 末尾追加:

```tsx
describe("AgentActivity recall", () => {
  it("渲染 KB 预召回事件(文档/章节)", () => {
    render(<AgentActivity events={[
      { type: "recall", source: "kb", hits: [
        { doc: "退换货政策", section: "七天无理由", score: 0.62 },
        { doc: "会员权益", section: "钻石会员", score: 0.41 },
      ] },
    ]} defaultOpen />);
    expect(screen.getByText(/退换货政策\/七天无理由/)).toBeInTheDocument();
    expect(screen.getByText(/会员权益\/钻石会员/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd webui && npx vitest run src/tests/chat-components.test.tsx`
Expected: FAIL(recall 事件未渲染,查不到文本)

- [ ] **Step 3: 改 AgentActivity.tsx**

在 lucide-react import 里加 `BookOpen`;在 `{e.type === "polish" && ...}` 行之后同级追加:

```tsx
              {e.type === "recall" && <><BookOpen className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>预召回 → 平台知识：{(e.hits as { doc: string; section: string }[] ?? []).map(h => `${h.doc}/${h.section}`).join("、")}</span></>}
```

- [ ] **Step 4: 改 ChatView.tsx 事件白名单**

把:

```tsx
      else if (["thought", "tool_call", "tool_result", "guard", "route", "select", "evaluate", "polish"].includes(e.type)) patch((t) => ({ ...t, activity: [...t.activity, e] }));
```

改为:

```tsx
      else if (["thought", "tool_call", "tool_result", "guard", "route", "select", "evaluate", "polish", "recall"].includes(e.type)) patch((t) => ({ ...t, activity: [...t.activity, e] }));
```

- [ ] **Step 5: 跑测试确认通过 + 类型检查**

Run: `cd webui && npx vitest run && npx tsc --noEmit`
Expected: 全部通过(8 tests),tsc 无输出

- [ ] **Step 6: 提交**

```bash
git add webui/src/components/AgentActivity.tsx webui/src/components/ChatView.tsx webui/src/tests/chat-components.test.tsx
git commit -m "feat: 思考过程面板展示 KB 预召回事件"
```

---

### Task 5: 提示词衔接 + 文档

**Files:**
- Modify: `app/prompts/agents.py`(三个域提示词里完全相同的政策强制检索规则句,64/104/144 行)
- Create: `docs/统一召回层-作用与效果.md`
- Test: 无新测试(提示词纯文案);跑全量回归收尾

**Interfaces:**
- Consumes: Task 1 注入段标头「【平台知识(自动检索)】」(提示词按此标头引用)
- Produces: 无代码接口

- [ ] **Step 1: 改提示词(replace_all,三处相同)**

在 `app/prompts/agents.py` 中,把三处相同的子串:

```
凭空讲会编错（如订单号格式、天数、运费承担）
```

全部替换为:

```
凭空讲会编错（如订单号格式、天数、运费承担）。系统每轮可能已自动注入【平台知识(自动检索)】段：该段覆盖所问要点时直接引用作答（无需重复检索）；未覆盖时仍须检索
```

- [ ] **Step 2: 写文档**

创建 `docs/统一召回层-作用与效果.md`:

```markdown
# 统一召回层(Unified Recall)——作用与效果

## 背景问题

KB 知识(退换货政策/会员权益/配送/FAQ)此前是纯工具式:模型自己决定调不调
`search_knowledge`。traces.db 实测 127 次工具调用中它只被调 5 次(约 4%)——
政策类问题模型经常不检索直接答,是"政策编造"(如编造 PX/YD 订单号格式)的
主要空档,只能靠回复流水线评估器兜底。

## 生产对齐:存储分离、召回统一

参考阿里小蜜/京东言犀模式:

- **存储分离**:用户记忆(一人一份、可写、隐私)与平台知识库(全局共享、
  只读、运营维护)物理独立,互不写入——写路径/权限/生命周期不混。
- **召回统一**:一个 Recall 接口(`app/agent/recall/service.py`)挂 N 个源,
  每轮回答前统一预检索、合并注入上下文:

  | 源 | 存储 | 门控 |
  |---|---|---|
  | profile 用户档案 | profile.db | memory_profile_enabled |
  | long_term 长期记忆 | memory/*.json + FTS5 | memory_enabled |
  | short_term 短期摘要 | 会话内 | memory_enabled |
  | kb 平台知识 | kb_index.json(向量) | recall_kb_enabled |

## 实现要点

- **KB 预召回源** `app/agent/recall/kb.py`:复用 `search_knowledge`,加相似度
  阈值(recall_kb_min_score=0.30)、片段数(top_k=2)、字符预算(1200)、短问题
  门控(≥4 字才检索);任何失败返回空,绝不阻塞回复。
- **每轮缓存**:`EcomAgent._turn_recall` 按本轮 last_user 缓存召回结果,
  react 循环多步组消息不重复 embedding。
- **单源隔离**:任一源抛错只丢该源(如 FTS 索引坏了,KB 照常注入)。
- **可观测**:KB 命中发 `recall` 事件——前端思考过程面板显示
  「预召回 → 平台知识:文档/章节」,tracer 落 `kind=recall` span,Langfuse 可查。
- **三层防线协同**:预召回(框架驱动,不依赖模型自觉)→ 强制检索提示词
  (预注入未覆盖时模型仍须调工具深查)→ 流水线评估器(最后兜底)。
  `search_knowledge` 工具保留:预注入管"常见问题秒答",工具管"追问式深查"。

## 效果

- 政策类问题的知识供给从"模型自觉(4% 触发)"变为"每轮必达(命中阈值即注入)"
- 政策编造空档显著收窄:模型手里始终有官方原文可引用
- 回滚开关:`recall_kb_enabled=false` 一键回到纯工具式
```

- [ ] **Step 3: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

Run: `cd webui && npx vitest run`
Expected: 全部通过

- [ ] **Step 4: 提交**

```bash
git add app/prompts/agents.py docs/统一召回层-作用与效果.md
git commit -m "docs: 提示词衔接预注入知识段 + 统一召回层文档"
```
