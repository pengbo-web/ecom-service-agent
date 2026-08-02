# 当前商品上下文(商品识别与介绍)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 顾客带着某个商品进入客服(链接 `?item=<hmdp商品id>` 或商品卡),AI 自动"识别"该商品并能回答"这是什么 / 多少钱 / 有货吗 / 有别的颜色吗"等指代问题——用**真实 hmdp 商品数据**接地,而非编造。

**Architecture:** 对标企业级客服的真相——**不是"看图识别",而是"结构化当前商品上下文"**。实现三段:①前端把 `current_item_id`(来自 URL/商品卡)随每轮消息传给 `/api/chat`;②后端用 ContextVar 把它透传到 Agent 线程(完全复用现有 `hmdp_token` 那套 `runtime_context` 模式);③Agent 在 `_build_messages()` 里按 id 从 hmdp 拉商品详情、拼成"【当前咨询商品】"系统块注入,让大模型对"这/它/这款"做**指代消解 + 接地生成**。深层查询仍可调已有 `query_product` 工具。

**Tech Stack:** Python/FastAPI + `contextvars`(已用)+ httpx(已依赖);hmdp `GET /product/{id}`(公开接口,议价已在用);React/Vite 前端;不新增第三方依赖。

## Global Constraints

- 不新增第三方依赖。
- 商品数据来源唯一 = hmdp `GET /product/{id}`;金额 hmdp 存**分**,展示 /100 为**元**。
- 商品上下文获取**失败一律降级**(返回 None → 不注入),不得抛错阻断对话(mock 模式/无 hmdp 时正常聊天)。
- 透传严格照现有 `hmdp_token` 模式:`ChatRequest` 字段 → `run_agent_streaming` 形参 → `streaming.worker` 里 `set_current_*` → Agent 读 ContextVar。**不信任模型传参**,current_item 只来自请求上下文。
- 每轮只拉一次商品详情(按 item_id 缓存),`_build_messages()` 一轮内多次调用不得重复请求 hmdp。
- 注入的系统块必须显式声明"'这/它/这款'默认指当前商品",实现指代消解。

---

## File Structure

**后端:**
- `app/api/schemas.py` — `ChatRequest` 加 `current_item_id`。
- `app/agent/runtime_context.py` — 加 `_current_item` ContextVar + set/get(与 `_current_token` 并列)。
- `app/api/streaming.py` — `run_agent_streaming` 加形参 + `worker()` 里 `set_current_item`。
- `app/api/app.py` — `/api/chat` 调 `run_agent_streaming` 时把 `req.current_item_id` 透传。
- `app/agent/product_context.py` — **新建**:`fetch_product_context(item_id) -> str | None`,GET hmdp 商品并拼"【当前咨询商品】"块。
- `app/agent/chat.py` — `_build_messages()` 注入当前商品块;加每轮缓存字段 `_turn_item_ctx`。
- `app/config/settings.py` — 加 `hmdp_base_url`(默认 `http://127.0.0.1:8085`)。

**前端:**
- `webui/src/hooks/useChatStream.ts` — `/api/chat` body 带 `current_item_id`。
- `webui/src/components/ChatView.tsx` — 从 `?item=` 读取并持有 `itemId`,随消息发送,顶部显示"正在咨询"横幅。

---

## Task 1: 后端 — 透传 `current_item_id`(schema + ContextVar + streaming)

**Files:**
- Modify: `app/api/schemas.py`
- Modify: `app/agent/runtime_context.py`
- Modify: `app/api/streaming.py`
- Modify: `app/api/app.py`
- Test: `tests/test_product_context.py`(新建)

**Interfaces:**
- Produces:
  - `ChatRequest.current_item_id: str = ""`
  - `runtime_context.set_current_item(item_id: Optional[str])` / `get_current_item() -> Optional[str]`
  - `run_agent_streaming(..., current_item_id: str = "")` —— worker 内 `set_current_item(current_item_id or None)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_product_context.py
from app.agent.runtime_context import set_current_item, get_current_item
from app.api.schemas import ChatRequest


def test_chat_request_has_current_item_id():
    r = ChatRequest(session_id="s", message="这是什么", current_item_id="155")
    assert r.current_item_id == "155"


def test_current_item_contextvar_roundtrip():
    set_current_item("155")
    assert get_current_item() == "155"
    set_current_item(None)
    assert get_current_item() is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_product_context.py -q`
Expected: FAIL(`set_current_item` 未定义 / schema 无该字段)

- [ ] **Step 3: 加 schema 字段**

`app/api/schemas.py` 的 `ChatRequest` 末尾加:

```python
    current_item_id: str = ""   # 当前咨询商品(hmdp 商品 id):前端从 ?item= 或商品卡带入,用于"这是什么"等指代
```

- [ ] **Step 4: 加 ContextVar**

`app/agent/runtime_context.py` 在 `_current_token` 定义后追加:

```python
# 当前咨询商品 id(顾客正在看的商品):用于"这/它/这款"的指代消解与商品介绍接地。
# 与 current_user/current_token 同模式:每轮由 streaming worker 刷新;不信任模型传参。
_current_item: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_item", default=None)


def set_current_item(item_id: Optional[str]) -> None:
    _current_item.set(item_id)


def get_current_item() -> Optional[str]:
    return _current_item.get()
```

- [ ] **Step 5: streaming worker 透传**

`app/api/streaming.py`:①在 `run_agent_streaming` 的形参表加 `current_item_id: str = ""`(与 `hmdp_token` 并列);②在 `worker()` 里紧接 `set_current_token(hmdp_token or None)` 之后加:

```python
        from app.agent.runtime_context import set_current_item
        set_current_item(current_item_id or None)
```

- [ ] **Step 6: `/api/chat` 透传**

`app/api/app.py` 的 `/api/chat` 里,调用 `run_agent_streaming(...)` 处(现传 `hmdp_token=req.hmdp_token`)追加参数:

```python
                    hitl=hitl, confirm=req.confirm, hmdp_token=req.hmdp_token,
                    current_item_id=req.current_item_id,
```

- [ ] **Step 7: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_product_context.py -q`
Expected: PASS(2 passed)

- [ ] **Step 8: 提交**

```bash
git add app/api/schemas.py app/agent/runtime_context.py app/api/streaming.py app/api/app.py tests/test_product_context.py
git commit -m "feat(product-ctx): 透传 current_item_id(schema+ContextVar+streaming)"
```

---

## Task 2: 后端 — 商品上下文抓取 `product_context.py`

**Files:**
- Create: `app/agent/product_context.py`
- Modify: `app/config/settings.py`
- Test: `tests/test_product_context.py`

**Interfaces:**
- Consumes: hmdp `GET {hmdp_base_url}/product/{item_id}` → `{success, data:{title, price(分), stock, specs, description}}`
- Produces: `fetch_product_context(item_id: str, client=None) -> str | None` —— 成功返回"【当前咨询商品】…"多行文本;任何失败(缺 id / 非200 / 无 data / 异常)返回 None。`client` 可注入(测试用 fake)。

- [ ] **Step 1: 写失败测试**

```python
import httpx
from app.agent.product_context import fetch_product_context


class _FakeResp:
    def __init__(self, js): self._js = js; self.status_code = 200
    def json(self): return self._js
    def raise_for_status(self): pass


class _FakeClient:
    def __init__(self, js): self._js = js
    def get(self, url, timeout=None): return _FakeResp(self._js)


def test_fetch_product_context_formats_block():
    js = {"success": True, "data": {"title": "过膝呢子大衣", "price": 30000,
          "stock": 5, "specs": "{\"颜色\":\"驼色\"}", "description": "宽松中长款"}}
    block = fetch_product_context("155", client=_FakeClient(js))
    assert block is not None
    assert "过膝呢子大衣" in block and "300" in block   # 分→元
    assert "这" in block                                 # 指代消解声明


def test_fetch_product_context_degrades_on_failure():
    assert fetch_product_context("", client=_FakeClient({})) is None
    assert fetch_product_context("155", client=_FakeClient({"success": False})) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_product_context.py -q -k fetch`
Expected: FAIL(模块不存在)

- [ ] **Step 3: 加 settings 字段**

`app/config/settings.py` 加(与其它 URL 配置并列):

```python
    hmdp_base_url: str = "http://127.0.0.1:8085"   # hmdp 后端(商品上下文按 id 取详情用)
```

- [ ] **Step 4: 实现 product_context.py**

```python
# app/agent/product_context.py
"""当前咨询商品上下文:按 hmdp 商品 id 拉详情,拼成注入 LLM 的"当前商品"块。

对标企业级客服:顾客从商品页/商品卡进客服,系统把该商品作为会话上下文,
AI 据此对"这/它/这款"做指代消解并接地介绍。数据来自 hmdp(公开 GET /product/{id}),
金额分→元。任何失败降级为 None(不注入,不影响对话)。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _yuan(fen) -> float:
    return round((fen or 0) / 100, 2)


def fetch_product_context(item_id: str, client=None) -> Optional[str]:
    if not item_id:
        return None
    try:
        from app.config.settings import settings
        base = settings.hmdp_base_url.rstrip("/")
        if client is None:
            import httpx
            client = httpx.Client(timeout=3.0)
        resp = client.get(f"{base}/product/{item_id}", timeout=3.0)
        data = resp.json() if resp.status_code == 200 else {}
        p = data.get("data") if data.get("success") else None
        if not p:
            return None
        specs = p.get("specs")
        try:
            specs = json.loads(specs) if isinstance(specs, str) else (specs or {})
        except (ValueError, TypeError):
            specs = {}
        spec_str = "、".join(f"{k}:{v}" for k, v in specs.items()) if specs else "—"
        return (
            "【当前咨询商品】(顾客正在看这件；顾客说"这/它/这款/这个"时默认指它)\n"
            f"- 名称：{p.get('title')}\n"
            f"- 价格：¥{_yuan(p.get('price'))}\n"
            f"- 库存：{p.get('stock')}\n"
            f"- 规格：{spec_str}\n"
            f"- 描述：{p.get('description') or '—'}\n"
            "回答"这是什么/多少钱/有货吗"等指代问题时,直接依据本商品作答;"
            "需要更多细节或下单/议价时可调用相应工具。"
        )
    except Exception:  # noqa: BLE001
        logger.warning("fetch_product_context 失败,降级不注入", exc_info=True)
        return None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_product_context.py -q -k fetch`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/agent/product_context.py app/config/settings.py tests/test_product_context.py
git commit -m "feat(product-ctx): 按 hmdp 商品 id 抓取当前商品上下文块"
```

---

## Task 3: 后端 — 在 `_build_messages()` 注入当前商品块(每轮缓存)

**Files:**
- Modify: `app/agent/chat.py`
- Test: `tests/test_product_context.py`

**Interfaces:**
- Consumes: `runtime_context.get_current_item()`、`product_context.fetch_product_context`
- Produces: 当 `get_current_item()` 非空时,`_build_messages()` 返回的消息里含一条 `role="system"` 且以"【当前咨询商品】"开头的块(位于主系统提示之后、召回/历史之前);同一 item_id 一轮内只抓一次(`self._turn_item_ctx = (item_id, block)`)。

- [ ] **Step 1: 写失败测试**

```python
import app.agent.product_context as pc
from app.agent.runtime_context import set_current_item
from app.multi_agent.orchestrator import MultiAgentOrchestrator


def test_build_messages_injects_current_product(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "fetch_product_context",
                        lambda item_id, client=None: "【当前咨询商品】名称：测试大衣" if item_id else None)
    set_current_item("155")
    agent = MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")
    engine = agent.engine
    engine.raw_messages = [{"role": "user", "content": "这是什么"}]
    msgs = engine._build_messages()
    assert any(m["role"] == "system" and "【当前咨询商品】" in m["content"] for m in msgs)
    set_current_item(None)
    msgs2 = engine._build_messages()
    assert not any("【当前咨询商品】" in m.get("content", "") for m in msgs2)
```

> 注:`MultiAgentOrchestrator` 的引擎属性名若非 `engine`,以实际为准(见 `app/multi_agent/orchestrator.py`);测试目标是"引擎 `_build_messages()` 注入商品块"。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_product_context.py -q -k build_messages`
Expected: FAIL(未注入)

- [ ] **Step 3: 加缓存字段**

`app/agent/chat.py` 的 `__init__` 里,`self._turn_recall = None` 附近加:

```python
        self._turn_item_ctx = None   # (item_id, 商品块) 每轮缓存:同商品不重复请求 hmdp
```

并在 `chat()` 每轮开头清缓存处(`self._turn_recall = None` 那行附近)加:

```python
        self._turn_item_ctx = None
```

- [ ] **Step 4: 注入商品块**

`app/agent/chat.py` 的 `_build_messages()` 里,构造初始 `messages`(`messages: list[dict] = [{"role": "system", "content": system_content}]`)之后、`last_user = ...` 之前,插入:

```python
        # 当前咨询商品上下文:顾客带商品进客服时注入,让"这/它/这款"指代消解到该商品并接地介绍。
        # 每轮按 item_id 缓存,避免一轮内多次 _build_messages 重复请求 hmdp。
        from app.agent.runtime_context import get_current_item
        item_id = get_current_item()
        if item_id:
            if self._turn_item_ctx is None or self._turn_item_ctx[0] != item_id:
                from app.agent.product_context import fetch_product_context
                self._turn_item_ctx = (item_id, fetch_product_context(item_id))
            block = self._turn_item_ctx[1]
            if block:
                messages.append({"role": "system", "content": block})
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_product_context.py -q`
Expected: PASS(全部)

- [ ] **Step 6: 相关回归(不引入破坏)**

Run: `.venv\Scripts\python.exe -m pytest tests/test_react_agent.py tests/test_orchestrator_unified.py tests/test_product_context.py -q -p no:cacheprovider --timeout=45`
Expected: 全绿(联网/LLM 用例若因无外网挂起,可只跑前述 3 个文件里的纯用例;记录跳过项)

- [ ] **Step 7: 提交**

```bash
git add app/agent/chat.py tests/test_product_context.py
git commit -m "feat(product-ctx): _build_messages 注入当前商品块(指代消解+接地)"
```

---

## Task 4: 前端 — `?item=` 带入 + 随消息发送 + "正在咨询"横幅

**Files:**
- Modify: `webui/src/hooks/useChatStream.ts`
- Modify: `webui/src/components/ChatView.tsx`

**Interfaces:**
- Consumes: Task 1 的 `/api/chat` `current_item_id` 字段。
- Produces:`useChatStream` 支持 `currentItemId` 并放进 `/api/chat` body;`ChatView` 从 `location.search` 的 `item` 读取并持有,发消息时带上,顶部显示"正在咨询商品 #<id>"横幅。

- [ ] **Step 1: useChatStream 带 current_item_id**

`webui/src/hooks/useChatStream.ts`:给 hook opts 加 `currentItemId?: string`,并把 `/api/chat` body 从:

```typescript
        body: JSON.stringify({ session_id: opts.sessionId, message, confirm, user_id: opts.userId }),
```

改为:

```typescript
        body: JSON.stringify({ session_id: opts.sessionId, message, confirm, user_id: opts.userId,
          current_item_id: opts.currentItemId || "" }),
```

并在 hook 参数类型里加 `currentItemId?: string`。

- [ ] **Step 2: ChatView 读取 ?item= 并传入 + 横幅**

`webui/src/components/ChatView.tsx`:

① 组件内加(与其它 useState 并列):

```typescript
  const [itemId] = useState<string>(() =>
    typeof location !== "undefined" ? (new URLSearchParams(location.search).get("item") || "") : "");
```

② 把 `useChatStream({ sessionId, userId, onEvent: ... })` 调用改为带 `currentItemId: itemId`:

```typescript
  const { send, streaming } = useChatStream({
    sessionId,
    userId,
    currentItemId: itemId,
    onEvent: (e) => { /* 原有逻辑不变 */
```

③ 在顶部工具条(`当前:<b>{userId}</b> · 会话 {sessionId}` 那一行所在的 header)下方,加一条商品横幅(仅当 itemId 非空):

```tsx
      {itemId && (
        <div className="flex items-center gap-2 border-b bg-primary/5 px-6 py-1.5 text-xs text-primary">
          🛍️ 正在咨询商品 <b>#{itemId}</b> —— 可直接问"这是什么 / 多少钱 / 有货吗"
        </div>
      )}
```

- [ ] **Step 3: 类型检查**

Run: `cd webui; npx tsc -b`
Expected: 无类型错误

- [ ] **Step 4: 提交**

```bash
git add webui/src/hooks/useChatStream.ts webui/src/components/ChatView.tsx
git commit -m "feat(product-ctx): 前端 ?item= 带入当前商品并随消息发送 + 咨询横幅"
```

---

## Task 5: 构建 + 端到端验证

**Files:**
- Modify: `web/dist/*`

- [ ] **Step 1: 构建前端**

Run: `cd webui; npm run build`
Expected: 输出到 `../web/dist`,无错误

- [ ] **Step 2: 重启 agent(载入新接口 + 新前端)**

Run: `powershell -File scripts\stop_all.ps1` 后 `powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1`(需 hmdp:8085 在跑)
Expected: 8010/8085/9123 UP

- [ ] **Step 3: 手动 E2E(浏览器)**

先在 hmdp 商品库取一个真实商品 id(例:`GET http://127.0.0.1:8085/product/list?keyword=` 里挑一个 id,或用商品列表页看到的 id)。

1. 打开 `http://127.0.0.1:8010/?user=1&item=<真实商品id>`。
2. 顶部应显示"🛍️ 正在咨询商品 #<id>"横幅。
3. 直接发 **"这是什么"** → AI 应说出**该商品**的名称/卖点(不是反问"您指哪件")。
4. 发 **"多少钱"** → 报**该商品**真实价格(¥,与 hmdp 一致)。
5. 发 **"有货吗"** → 依据真实库存回答。
6. 后端日志应见 `/api/chat` 携带该 item;`GET /product/<id>` 被 agent 侧调用一次/轮。

Expected:1–6 全部成立;换一个 `item=` 值,回答随之变成对应商品。

- [ ] **Step 4: 提交构建产物 + 推送**

```bash
git add web/dist
git commit -m "build(product-ctx): 重建前端产物"
git push origin feature/w1-service-streaming
```

---

## Self-Review

**1. Spec coverage:**
- "链接识别商品" → Task 4(`?item=` 带入)✓
- "会话绑定当前商品" → Task 1(ContextVar 透传)✓
- "查真实商品数据" → Task 2(hmdp GET /product/{id})✓
- "介绍/指代消解(这是什么/多少钱)" → Task 3(注入商品块 + 指代声明)✓
- "失败降级不阻断" → Task 2 全 try/except → None ✓
- "端到端可体验" → Task 5 ✓

**2. Placeholder 扫描:** 各步均有真实代码/命令/预期,无 TODO/占位;Task 3 对 orchestrator 引擎属性名标注了"以实际为准"并给出核对指引。

**3. 类型/命名一致性:** `current_item_id`(下划线,后端+API)↔ `currentItemId`(前端 camel);`set_current_item/get_current_item` 前后引用一致;`fetch_product_context(item_id, client=None)` 签名在 Task 2 定义、Task 3 引用一致;商品块以"【当前咨询商品】"开头,测试与实现同串。

**4. 实现时留意:**
- `run_agent_streaming` 的实际形参顺序/是否 **kwargs,以 `app/api/streaming.py` 现签名为准追加 `current_item_id`。
- `MultiAgentOrchestrator` 暴露引擎的属性名(`engine`/`self.engine`)以 `orchestrator.py` 为准;`_build_messages`/`_turn_recall` 在 `EcomAgent`(`chat.py`)上。
- hmdp `GET /product/{id}` 返回字段名(title/price/stock/specs/description)以 `mcp_server/hmdp_mapping.map_product` 读到的为准(price 为分)。
- 商品块里的中文引号务必转义正确(源码字符串),避免语法错。
