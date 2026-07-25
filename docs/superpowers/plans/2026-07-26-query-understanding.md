# 统一查询理解节点(意图识别) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对齐生产客服(阿里小蜜语义理解层/五步工作流①②):上游一个查询理解节点,一次 LLM 调用输出 `domain/intent/need_kb/kb_query`,同时吃掉现有的独立路由调用与改写调用——闲聊轮零检索开销,知识轮照常必达,路由/门控/改写单一信号源。

**Architecture:** 新增 `app/agent/understanding.py`(三层:规则快筛 0 成本 → LLM 严格 JSON → 失败兜底 need_kb=True);`MultiAgentOrchestrator.chat` 在 `query_understanding_enabled`(默认开)下用它替代 `Router.route`,并把结果经 `engine.set_turn_understanding(qu)` 注入引擎;`_build_messages` 消费 `qu.need_kb/kb_query` 决定检索与查询(替掉内联 rewrite 调用);`build_recall_sections` 增加 `include_kb` 参数;route 事件带 intent/need_kb,recall 事件新增 skipped 形态,前端可见。`rewrite.py` 被吸收后删除;`Router` 保留为回滚路径(关开关=老路由+每轮必检索)。

**Tech Stack:** Python 3.11 + pytest;React + vitest。不新增依赖。

## Global Constraints

- Python 一律 `.venv/Scripts/python.exe`;pytest:`.venv/Scripts/python.exe -m pytest <files> -q`;前端 `cd webui && npx vitest run` 与 `npx tsc --noEmit`
- Windows gbk 控制台:测试/脚本不 print emoji
- 提交信息末尾:`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;不推送;`.env` 严禁提交
- 容错红线:查询理解任何失败(LLM 挂/超时/JSON 解析错/domain 非法)必须兜底为 `need_kb=True + kb_query=原句 + domain=None(沿用上轮路由)`——错误方向永远偏"多检索、走默认路由",绝不阻塞回复
- 规则快筛红线:只判定"确定不需要知识"的短消息(≤20 字)模式,宁漏(交给 LLM)勿错杀;不与 API 层 fast_path 重复承担秒回职责(fast_path 在 app.py:177 已拦纯问候/感谢/告别并直接回复,到不了本节点)
- 测试环境红线:`tests/conftest.py` 必须钉 `query_understanding_enabled=False`(既有 orchestrator 测试 patch 的是 `o.router.route`,老路径必须保持可用且无网络调用);QU 相关新测试自行开 True 并 mock LLM
- 现有接口保持:`build_recall_sections` 新参数 `include_kb` 必须带默认值 True(既有调用零改动);`kb_recall(query) -> KbRecall` 不动
- 事件契约(前端依赖):route 事件新增可选字段 `intent/need_kb/source`(无则按旧样式渲染);recall 事件新增 skipped 形态 `{"type":"recall","source":"kb","skipped":true,"reason":<intent>}`
- 工作目录:`D:\2026项目\ecom-service-agent`(git 仓库根)

---

### Task 1: 查询理解节点 understanding.py

**Files:**
- Create: `app/agent/understanding.py`
- Modify: `app/config/settings.py`(`aperag_timeout_s` 之后加 1 项)
- Modify: `tests/conftest.py`(autouse fixture 钉 `query_understanding_enabled=False`,按既有钉法与还原风格)
- Test: `tests/test_understanding.py`

**Interfaces:**
- Consumes: OpenAI 兼容 client(`client.chat.completions.create`,可能不接受 per-request timeout → TypeError 降级重调);`settings.recall_kb_timeout_s`(复用作理解调用超时)
- Produces: `understand(user_input: str, history: list[dict], client, model: str) -> QueryUnderstanding`;`QueryUnderstanding(domain: str | None, intent: str, need_kb: bool, kb_query: str | None, source: str)`,source ∈ rule/llm/fallback。Task 2 的 chat 消费 need_kb/kb_query;Task 3 的 orchestrator 消费 domain/intent/source。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_understanding.py`:

```python
"""查询理解节点:规则快筛/LLM JSON 解析/全兜底。"""

from types import SimpleNamespace

from app.agent.understanding import QueryUnderstanding, understand


def _client(reply=None, raises=False, capture=None):
    def create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if raises:
            raise RuntimeError("llm down")
        msg = SimpleNamespace(content=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _hist(*users):
    out = []
    for u in users:
        out.append({"role": "user", "content": u})
        out.append({"role": "assistant", "content": "好的"})
    return out


# ---- 规则快筛(不调 LLM) ----

def test_rule_ack_words_skip_llm():
    c = _client(raises=True)                      # 若真调 LLM 会抛
    for text in ["嗯", "好的", "OK", "收到", "明白了"]:
        qu = understand(text, [], c, "m")
        assert qu.need_kb is False and qu.source == "rule"
        assert qu.intent == "闲聊寒暄" and qu.domain is None


def test_rule_pure_order_id():
    qu = understand("ORD-20240115-001", [], _client(raises=True), "m")
    assert qu.need_kb is False and qu.intent == "订单事务" and qu.source == "rule"


def test_rule_human_handoff():
    qu = understand("转人工", [], _client(raises=True), "m")
    assert qu.need_kb is False and qu.intent == "转人工" and qu.source == "rule"


def test_rule_only_for_short_text():
    """超 20 字即使含语气词也交给 LLM(此处 LLM 失败走兜底,证明没走规则层)。"""
    long_text = "好的好的,那我想再问一下退货的运费到底是谁来承担这个费用呢"
    qu = understand(long_text, [], _client(raises=True), "m")
    assert qu.source == "fallback" and qu.need_kb is True


# ---- LLM 路径 ----

def test_llm_full_parse(monkeypatch):
    cap = {}
    c = _client(reply='{"domain": "aftersale", "intent": "政策咨询", "need_kb": true, "kb_query": "退货运费谁承担"}',
                capture=cap)
    qu = understand("那运费呢?", _hist("退货政策是什么"), c, "m")
    assert qu == QueryUnderstanding(domain="aftersale", intent="政策咨询",
                                    need_kb=True, kb_query="退货运费谁承担", source="llm")
    sent = str(cap["messages"])
    assert "退货政策是什么" in sent and "那运费呢?" in sent   # 历史与原句都送给了 LLM


def test_llm_code_fence_tolerated():
    c = _client(reply='```json\n{"domain": "presale", "intent": "商品咨询", "need_kb": false, "kb_query": null}\n```')
    qu = understand("这双鞋有42码吗", [], c, "m")
    assert qu.domain == "presale" and qu.need_kb is False and qu.kb_query is None


def test_llm_invalid_domain_becomes_none():
    c = _client(reply='{"domain": "unknown", "intent": "其他", "need_kb": true, "kb_query": "x"}')
    qu = understand("随便问问", [], c, "m")
    assert qu.domain is None and qu.source == "llm"     # 非法 domain 置 None,粘性路由接管


def test_llm_need_kb_without_query_uses_raw():
    c = _client(reply='{"domain": "aftersale", "intent": "政策咨询", "need_kb": true, "kb_query": null}')
    qu = understand("价保多久", [], c, "m")
    assert qu.kb_query == "价保多久"                     # need_kb 但没给查询 → 原句兜底


# ---- 兜底 ----

def test_llm_error_falls_back_open():
    qu = understand("退货运费谁出", [], _client(raises=True), "m")
    assert qu.need_kb is True and qu.kb_query == "退货运费谁出"
    assert qu.domain is None and qu.source == "fallback"


def test_bad_json_falls_back_open():
    qu = understand("退货运费谁出", [], _client(reply="我觉得应该检索"), "m")
    assert qu.need_kb is True and qu.source == "fallback"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_understanding.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'app.agent.understanding'`

- [ ] **Step 3: 加配置与 conftest 钉桩**

`app/config/settings.py` 在 `aperag_timeout_s` 行之后插入:

```python
    # 统一查询理解节点(意图识别):一次 LLM 调用出 domain/intent/need_kb/kb_query,
    # 吃掉独立路由与改写调用;闲聊轮免检索。关=回退老 Router 路由+每轮必检索
    # (改写能力已并入本节点,回退路径检索用原句)
    query_understanding_enabled: bool = True
```

`tests/conftest.py`:读该文件,找到 autouse 钉设置的 fixture,按其既有风格加一行钉 `settings.query_understanding_enabled = False`(并在 teardown 还原,与文件里其它设置项做法完全一致)。

- [ ] **Step 4: 写实现**

创建 `app/agent/understanding.py`:

```python
"""统一查询理解节点(Query Understanding):意图识别+路由+检索门控+查询改写,一次调用。

对齐生产客服(阿里小蜜语义理解层/五步工作流①②):上游一个节点输出
domain/intent/need_kb/kb_query,下游各取所需——orchestrator 拿 domain 切画像,
召回层拿 need_kb/kb_query 决定检索。不再让"查不查知识库"依赖模型自觉,
也不再为路由和改写各花一次 LLM 调用(原两次上游调用合并为至多一次)。

三层结构:
  ① 规则快筛(0 成本,≤20 字):确认语气词/纯订单号/转人工 → 直接判定不调 LLM
     (纯问候/感谢在更上游的 API fast_path 已秒回,到不了这里;规则宁漏勿错杀)
  ② LLM 查询理解:一次调用输出严格 JSON
  ③ 兜底:任何失败 → need_kb=True + kb_query=原句 + domain=None(粘性路由接管)
     ——错误方向永远偏"多检索、走默认",绝不阻塞回复主流程。
"""

import json
import logging
import re
from dataclasses import dataclass

from app.config.settings import settings

logger = logging.getLogger(__name__)

VALID_DOMAINS = {"presale", "midsale", "aftersale"}
_RULE_MAX_CHARS = 20


@dataclass
class QueryUnderstanding:
    domain: str | None = None      # None=未判定,orchestrator 沿用上轮路由(粘性)
    intent: str = "其他"           # 政策咨询/商品咨询/订单事务/闲聊寒暄/投诉/转人工/其他
    need_kb: bool = True           # 检索门控:False=本轮跳过 KB 预召回
    kb_query: str | None = None    # need_kb 时的自包含检索查询(已消解指代/省略)
    source: str = "llm"            # rule/llm/fallback,供 route 事件与观测


_RULE_TABLE = [
    ("闲聊寒暄", re.compile(r"^(嗯+|哦+|噢|好的?|好嘞|行吧?|可以|ok|okay|收到|明白了?|知道了)[\s!！~。.,，]*$", re.IGNORECASE)),
    ("闲聊寒暄", re.compile(r"^(你好|您好|哈喽|嗨|在吗|再见|拜拜|谢谢|感谢)[\s!！~。.,，]*$", re.IGNORECASE)),
    ("订单事务", re.compile(r"^ORD-\d{8}-\d{3}$", re.IGNORECASE)),
    ("转人工", re.compile(r"^(转人工|人工客服|找人工|叫真人|人工)[\s!！~。.]*$")),
]

_QU_PROMPT = """你是电商客服的查询理解模块。分析用户最新消息,输出严格 JSON(不要任何解释、不要代码块):
{{"domain": "presale|midsale|aftersale", "intent": "政策咨询|商品咨询|订单事务|闲聊寒暄|投诉|其他", "need_kb": true或false, "kb_query": "自包含检索查询或null"}}

domain(路由,选最主要的):
- presale: 下单前——商品推荐/商品信息/价格/库存/活动优惠/优惠券/议价
- midsale: 订单进行中——查订单/物流/催发货/改收货地址/取消订单
- aftersale: 收货后或交易后——退换货/退款/发票/质量投诉/赔偿;打招呼闲聊账户问题默认归此

need_kb(是否需要检索平台知识库):
- true: 涉及平台政策/规则/流程/时效/费用/权益/售后标准(如"运费谁出""价保多久""怎么退货""发票怎么开")
- false: 纯订单操作(查单号/物流)/纯商品参数/闲聊寒暄/情绪宣泄——这些靠工具或对话即可

kb_query(need_kb=true 时必填):结合最近对话把指代和省略补全成自包含查询,
如上文聊退货、用户问"那运费呢?"→"退货运费谁承担";need_kb=false 时为 null。

最近对话(用户侧):
{context}

用户最新消息:{user_input}"""


def understand(user_input: str, history: list[dict], client, model: str) -> QueryUnderstanding:
    """三层查询理解;任何失败兜底为"多检索、走默认"。"""
    text = (user_input or "").strip()
    if len(text) <= _RULE_MAX_CHARS:
        for intent, pat in _RULE_TABLE:
            if pat.match(text):
                return QueryUnderstanding(intent=intent, need_kb=False, source="rule")

    users = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    context = "\n".join(f"- {u}" for u in users[-5:] if u) or "(无)"
    req = dict(
        model=model, temperature=0.0, max_tokens=150,
        messages=[{"role": "user",
                   "content": _QU_PROMPT.format(context=context, user_input=text)}],
    )
    try:
        try:
            resp = client.chat.completions.create(
                timeout=settings.recall_kb_timeout_s, **req)
        except TypeError:      # 代理不接受 per-request timeout
            resp = client.chat.completions.create(**req)
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        domain = data.get("domain")
        if domain not in VALID_DOMAINS:
            domain = None          # 非法 domain 不硬猜,交给粘性路由
        need_kb = bool(data.get("need_kb", True))
        kb_query = (str(data.get("kb_query") or "")).strip() or None
        if need_kb and not kb_query:
            kb_query = text        # 说要检索但没给查询 → 原句兜底
        return QueryUnderstanding(
            domain=domain, intent=str(data.get("intent") or "其他"),
            need_kb=need_kb, kb_query=kb_query if need_kb else None, source="llm")
    except Exception:
        logger.warning("query understanding failed, fallback to need_kb=True", exc_info=True)
        return QueryUnderstanding(source="fallback", kb_query=text or None)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_understanding.py -q`
Expected: 10 passed

- [ ] **Step 6: 提交**

```bash
git add app/agent/understanding.py app/config/settings.py tests/conftest.py tests/test_understanding.py
git commit -m "feat: 统一查询理解节点(规则快筛+LLM意图/门控/改写一次出+全兜底)"
```

---

### Task 2: 召回层消费 QU(检索门控落地,吸收 rewrite)

**Files:**
- Modify: `app/agent/recall/service.py`(`build_recall_sections` 加 `include_kb: bool = True`)
- Modify: `app/agent/chat.py`(`__init__` 加 `_turn_qu`;新增 `set_turn_understanding`;`_build_messages` 召回块消费 QU、删除 rewrite 调用;skipped 事件)
- Delete: `app/agent/recall/rewrite.py`、`tests/test_recall_rewrite.py`(能力已并入 QU)
- Test: `tests/test_recall_service.py`(追加)、`tests/test_recall_wiring.py`(改写相关用例重写为 QU 形态)

**Interfaces:**
- Consumes: Task 1 的 `QueryUnderstanding`(仅 need_kb/kb_query/intent 三字段)
- Produces: `EcomAgent.set_turn_understanding(qu: QueryUnderstanding | None) -> None`(Task 3 的 orchestrator 每轮调用);`build_recall_sections(memory_manager, query, include_kb=True) -> RecallResult`,`include_kb=False` 时跳过 KB 源且 `kb_backend="skipped"`;skipped recall 事件 `{"type":"recall","source":"kb","skipped":True,"reason":<intent>}`(Task 4 前端消费)

- [ ] **Step 1: 写失败测试**

`tests/test_recall_service.py` 末尾追加:

```python
def test_include_kb_false_skips_kb_entirely(monkeypatch):
    """检索门控:include_kb=False 时连 kb_recall 都不调,记忆源照常。"""
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    called = []
    monkeypatch.setattr(svc, "kb_recall", lambda q: called.append(q))
    r = svc.build_recall_sections(_mm(), "好的", include_kb=False)
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要"]
    assert r.kb_hits == [] and r.kb_backend == "skipped"
    assert called == []
```

`tests/test_recall_wiring.py`:

2a. 把 `test_recall_uses_rewritten_query_and_event_carries_it` **整体替换**为:

```python
def test_recall_uses_qu_kb_query_and_event_carries_it(monkeypatch):
    """QU 给出的自包含查询喂给召回,事件带实际检索查询。"""
    from app.agent.understanding import QueryUnderstanding
    calls = []

    def fake_recall(mm, query, include_kb=True):
        calls.append((query, include_kb))
        return RecallResult(
            sections=[{"role": "system", "content": "【平台知识(自动检索)】X"}],
            kb_hits=[{"doc": "d", "section": "s", "score": 0.9}],
        )

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.set_turn_understanding(QueryUnderstanding(
        domain="aftersale", intent="政策咨询", need_kb=True,
        kb_query="退货运费谁承担", source="llm"))
    agent.raw_messages.append({"role": "user", "content": "那运费呢?"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == [("退货运费谁承担", True)]
    ev = [e for e in events if e["type"] == "recall"][0]
    assert ev["query"] == "退货运费谁承担" and not ev.get("skipped")


def test_qu_need_kb_false_skips_and_emits_skipped(monkeypatch):
    """门控关检索:include_kb=False 传入召回,发 skipped 事件。"""
    from app.agent.understanding import QueryUnderstanding
    calls = []

    def fake_recall(mm, query, include_kb=True):
        calls.append((query, include_kb))
        return RecallResult(kb_backend="skipped")

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.set_turn_understanding(QueryUnderstanding(
        intent="闲聊寒暄", need_kb=False, source="rule"))
    agent.raw_messages.append({"role": "user", "content": "好的"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == [("好的", False)]          # 记忆查询仍用原句
    ev = [e for e in events if e["type"] == "recall"][0]
    assert ev["skipped"] is True and ev["reason"] == "闲聊寒暄"


def test_no_qu_defaults_to_old_behavior(monkeypatch):
    """引擎独立运行(无 orchestrator 注入 QU):原句检索,include_kb=True。"""
    calls = []

    def fake_recall(mm, query, include_kb=True):
        calls.append((query, include_kb))
        return RecallResult()

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    agent.event_sink = lambda e: None
    agent.raw_messages.append({"role": "user", "content": "退货政策"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == [("退货政策", True)]
```

2b. 既有三个测试(`test_build_messages_injects_and_caches_per_turn` / `test_new_user_turn_recomputes` / `test_no_hits_no_event`)里的
`monkeypatch.setattr("app.agent.recall.rewrite.rewrite_for_recall", ...)` 行**全部删除**(rewrite 已不存在);同时它们 mock 的 `fake_recall` 函数签名改为 `def fake(mm, query, include_kb=True):`(多收一个参数,断言不变)。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_service.py tests/test_recall_wiring.py -q`
Expected: FAIL(`include_kb` 参数不存在 / `set_turn_understanding` 属性不存在)

- [ ] **Step 3: 改 service.py**

`build_recall_sections` 签名与 KB 段改为:

```python
def build_recall_sections(memory_manager, query: str | None,
                          include_kb: bool = True) -> RecallResult:
    """统一召回入口:按源顺序检索,合并为注入段列表;单源失败隔离。
    include_kb=False(查询理解判定本轮无需知识)时跳过 KB 源,记忆源照常。"""
    result = RecallResult()
    memory_on = memory_manager is not None and getattr(memory_manager, "memory_enabled", False)
    if memory_on:
        for source in (_profile_section, _long_term_section, _short_term_section):
            try:
                text = source(memory_manager, query)
            except Exception:
                logger.warning("recall source %s failed", source.__name__, exc_info=True)
                continue
            if text:
                result.sections.append({"role": "system", "content": text})
    if not include_kb:
        result.kb_backend = "skipped"
        return result
    try:
        kb = kb_recall(query)
    except Exception:
        logger.warning("recall source kb failed", exc_info=True)
        kb = KbRecall()
    result.kb_backend = kb.backend
    if kb.section:
        result.sections.append({"role": "system", "content": kb.section})
        result.kb_hits = kb.hits
    return result
```

- [ ] **Step 4: 改 chat.py**

4a. `__init__` 里 `self._turn_recall = None` 行之后加:

```python
        self._turn_qu = None       # 查询理解结果(orchestrator 每轮注入;引擎独立运行时 None=老行为)
```

4b. 类中新增方法(放在 `set_*` 系方法附近或 `chat` 之前):

```python
    def set_turn_understanding(self, qu) -> None:
        """orchestrator 每轮注入查询理解结果(QueryUnderstanding);None=退回默认行为。"""
        self._turn_qu = qu
```

4c. `_build_messages` 召回块整体替换为(删除 rewrite 的 import 与调用):

```python
        # 统一召回层:profile/LTM/STM/KB 四源一次装配(存储分离、召回统一)。
        # 检索门控与查询改写来自上游查询理解节点(qu);每轮缓存防重复检索。
        from app.agent.recall.service import build_recall_sections
        if self._turn_recall is None or self._turn_recall[0] != last_user:
            qu = self._turn_qu
            include_kb = qu.need_kb if qu is not None else True
            recall_query = (qu.kb_query if qu is not None and qu.kb_query else last_user)
            rr = build_recall_sections(self.memory_manager, recall_query,
                                       include_kb=include_kb)
            self._turn_recall = (last_user, rr)
            if rr.kb_hits:   # 命中才发正常事件(前端思考面板+tracer 各消费一次)
                self._emit({"type": "recall", "source": "kb", "backend": rr.kb_backend,
                            "query": recall_query, "hits": rr.kb_hits})
            elif qu is not None and not qu.need_kb:
                # 门控跳过:显式发 skipped 事件,门控工作与否前端一眼可见
                self._emit({"type": "recall", "source": "kb",
                            "skipped": True, "reason": qu.intent})
        messages.extend(self._turn_recall[1].sections)
```

- [ ] **Step 5: 删除被吸收的模块**

```bash
git rm app/agent/recall/rewrite.py tests/test_recall_rewrite.py
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_service.py tests/test_recall_wiring.py tests/test_recall_kb.py tests/test_external_kb.py tests/test_understanding.py -q`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add -A app/agent/recall/ app/agent/chat.py tests/
git commit -m "feat: 召回层消费查询理解结果(检索门控+skipped事件),rewrite 并入 QU 后删除"
```

---

### Task 3: orchestrator 接入 QU(替代 Router,粘性路由,事件增强)

**Files:**
- Modify: `app/multi_agent/orchestrator.py`(`__init__` 加 `_last_key`;`chat` 前半段改造)
- Test: `tests/test_orchestrator_qu.py`(新)

**Interfaces:**
- Consumes: Task 1 `understand(...) -> QueryUnderstanding`;Task 2 `engine.set_turn_understanding(qu)`;`app.multi_agent.router.DEFAULT_AGENT`(="aftersale",已存在);`settings.query_understanding_enabled`
- Produces: route 事件 `{"type":"route","agent":<名>,"key":<域>,"intent":<意图>,"need_kb":<bool>,"source":<rule/llm/fallback>}`(开 QU 时;关时保持旧三字段)。Task 4 前端消费。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_orchestrator_qu.py`:

```python
"""orchestrator 接入查询理解:路由来源/粘性/事件增强/回滚路径。"""

from app.agent.recall.service import RecallResult
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings
from app.multi_agent.orchestrator import MultiAgentOrchestrator


def _orch(tmp_path, monkeypatch):
    o = MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")
    # 引擎不触网:react 返回固定结构化文本;流水线原样透传;记忆更新打桩;
    # 召回打桩(chat 轮末 token 估算会真调 _build_messages→召回,不桩会打真网络)
    o.engine._react_loop = lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}'
    o.engine.memory_manager.update_short_term = lambda *a, **k: None
    o.engine._reply_pipeline.run = lambda *a, **k: a[3]   # 原样返回草稿,不走流水线
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True: RecallResult())
    return o


def test_qu_domain_routes_and_event_enriched(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    qu = QueryUnderstanding(domain="presale", intent="商品咨询",
                            need_kb=False, source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", lambda *a, **k: qu)
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("这双鞋怎么样")
    route = [e for e in events if e["type"] == "route"][0]
    assert route["key"] == "presale"
    assert route["intent"] == "商品咨询" and route["need_kb"] is False and route["source"] == "llm"
    assert o.engine._turn_qu is qu                        # QU 注入了引擎


def test_domain_none_sticky_to_last_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    seq = [QueryUnderstanding(domain="midsale", intent="订单事务", need_kb=False, source="llm"),
           QueryUnderstanding(domain=None, intent="闲聊寒暄", need_kb=False, source="rule")]
    monkeypatch.setattr("app.agent.understanding.understand",
                        lambda *a, **k: seq.pop(0))
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("查一下订单")
    o.chat("好的")
    routes = [e for e in events if e["type"] == "route"]
    assert routes[0]["key"] == "midsale"
    assert routes[1]["key"] == "midsale"                  # 粘住上一轮,不跳默认域


def test_domain_none_first_turn_uses_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr("app.agent.understanding.understand",
                        lambda *a, **k: QueryUnderstanding(domain=None, intent="其他",
                                                           need_kb=True, kb_query="x",
                                                           source="fallback"))
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("嗯?")
    assert [e for e in events if e["type"] == "route"][0]["key"] == "aftersale"


def test_disabled_falls_back_to_router(tmp_path, monkeypatch):
    """回滚路径:关开关走老 Router,事件保持旧形态,引擎 QU 为 None。"""
    monkeypatch.setattr(settings, "query_understanding_enabled", False)
    o = _orch(tmp_path, monkeypatch)
    monkeypatch.setattr(o.router, "route", lambda *a, **k: "aftersale")
    events = []
    o.event_sink = events.append
    o.chat("退货")
    route = [e for e in events if e["type"] == "route"][0]
    assert route["key"] == "aftersale" and "intent" not in route
    assert o.engine._turn_qu is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_orchestrator_qu.py -q`
Expected: FAIL(route 事件无 intent 字段 / `_turn_qu` 未注入 / 粘性不生效)

- [ ] **Step 3: 改 orchestrator.py**

3a. import 区确认有 `from app.multi_agent.router import Router` —— 改为同时引入默认域:

```python
from app.multi_agent.router import DEFAULT_AGENT, Router
```

3b. `__init__` 里 `self.router = Router(...)` 行之后加:

```python
        self._last_key: str | None = None   # 粘性路由:QU 未判定 domain 时沿用上轮
```

3c. `chat` 方法开头(原 `key = self.router.route(...)` 与 route 事件两行)替换为:

```python
    def chat(self, user_input: str):
        # 统一查询理解(默认):一次调用出 domain/intent/need_kb/kb_query,
        # 替代独立路由;关开关=回退老 Router(每轮必检索,无门控无改写)
        if settings.query_understanding_enabled:
            from app.agent import understanding
            qu = understanding.understand(user_input, self.engine.raw_messages,
                                          self.engine.client, self.engine.model)
            key = qu.domain or self._last_key or DEFAULT_AGENT
        else:
            qu = None
            key = self.router.route(user_input, self.engine.raw_messages)
        self._last_key = key
        self.engine.set_turn_understanding(qu)
        profile = self.profiles.get(key) or next(iter(self.profiles.values()))
        if self.event_sink:
            event = {"type": "route", "agent": profile["name"], "key": key}
            if qu is not None:
                event.update(intent=qu.intent, need_kb=qu.need_kb, source=qu.source)
            self.event_sink(event)
```

(注意 monkeypatch 兼容:`understand` 通过 `from app.agent import understanding` + `understanding.understand(...)` 调用,测试 patch `"app.agent.understanding.understand"` 才能生效。其余 `chat` 方法体——切画像/透传/`engine.chat`——保持原样。)

- [ ] **Step 4: 跑测试确认通过 + 既有回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_orchestrator_qu.py tests/test_orchestrator_unified.py tests/test_controller_agent.py -q`
Expected: 全部通过(conftest 钉 False,既有测试走老路径不受影响)

- [ ] **Step 5: 提交**

```bash
git add app/multi_agent/orchestrator.py tests/test_orchestrator_qu.py
git commit -m "feat: orchestrator 接入查询理解节点(替代独立路由调用,粘性路由,route 事件带意图/门控)"
```

---

### Task 4: 前端展示 + 文档 + 全量回归 + 浏览器自测

**Files:**
- Modify: `webui/src/components/AgentActivity.tsx`(route 行加意图/门控徽标;recall 行拆 skipped/正常两分支)
- Modify: `webui/src/tests/chat-components.test.tsx`(追加两用例)
- Modify: `docs/统一召回层-作用与效果.md`(追加查询理解章节)
- Test: vitest + tsc + 后端回归子集 + 浏览器端到端自测

**Interfaces:**
- Consumes: route 事件可选字段 `intent/need_kb`;recall 事件 skipped 形态 `{"skipped":true,"reason":...}`

- [ ] **Step 1: 写失败测试**

`webui/src/tests/chat-components.test.tsx` 追加:

```tsx
describe("AgentActivity 查询理解", () => {
  it("route 事件带意图与门控徽标", () => {
    render(<AgentActivity events={[
      { type: "route", agent: "售后服务专家", key: "aftersale", intent: "政策咨询", need_kb: true, source: "llm" },
    ]} defaultOpen />);
    expect(screen.getByText(/政策咨询/)).toBeInTheDocument();
    expect(screen.getByText(/需检索/)).toBeInTheDocument();
  });

  it("recall skipped 事件渲染跳过原因", () => {
    render(<AgentActivity events={[
      { type: "recall", source: "kb", skipped: true, reason: "闲聊寒暄" },
    ]} defaultOpen />);
    expect(screen.getByText(/跳过/)).toBeInTheDocument();
    expect(screen.getByText(/闲聊寒暄/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd webui && npx vitest run src/tests/chat-components.test.tsx`
Expected: FAIL(徽标/跳过文案不存在)

- [ ] **Step 3: 改 AgentActivity.tsx**

3a. route 行(现为 `{e.type === "route" && ...路由 → <b>...</b> Agent...}`)替换为:

```tsx
              {e.type === "route" && <><Route className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>路由 → <b>{ROUTE_LABEL[String(e.key)] ?? e.key}</b> Agent{e.intent ? <span className="text-muted-foreground">（{e.intent} · {e.need_kb ? "需检索" : "免检索"}）</span> : null}</span></>}
```

3b. recall 行拆为两分支(替换现有单行):

```tsx
              {e.type === "recall" && e.skipped && <><BookOpen className="mt-0.5 h-3.5 w-3.5 text-muted-foreground" /><span className="text-muted-foreground">预召回 → 跳过（{e.reason}，无需检索知识库）</span></>}
              {e.type === "recall" && !e.skipped && <><BookOpen className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>预召回<b>[{e.backend === "aperag" ? "ApeRAG" : "本地索引"}]</b>{e.query ? `（查询：${e.query}）` : ""} → 平台知识：{(e.hits as { doc: string; section: string }[] ?? []).map(h => `${h.doc}/${h.section}`).join("、")}</span></>}
```

- [ ] **Step 4: 前端测试与类型检查**

Run: `cd webui && npx vitest run && npx tsc --noEmit`
Expected: 全部通过(10 tests),tsc 无输出

- [ ] **Step 5: 文档追加**

`docs/统一召回层-作用与效果.md` 末尾追加:

```markdown
## 升级:统一查询理解节点(意图门控检索)

预召回原先每轮必触发(仅长度门控)——闲聊轮也要花一次改写调用 + 一次检索,
且路由(Router)与改写(rewrite)各占一次上游 LLM 调用。对齐阿里小蜜语义理解层
与生产五步工作流①②,合并为**一个查询理解节点**(`app/agent/understanding.py`):

    ① 规则快筛(0 成本,≤20字): 确认语气词/纯订单号/转人工 → 直接判定
       (纯问候/感谢在更上游 API fast_path 已秒回)
    ② LLM 查询理解(一次调用,严格 JSON):
       {domain: 路由, intent: 意图, need_kb: 检索门控, kb_query: 自包含改写查询}
    ③ 兜底: 任何失败 → need_kb=true + 原句检索 + 沿用上轮路由(粘性)

- 上游 LLM 调用:路由 1 次 + 改写 1 次 → **至多 1 次**(规则命中 0 次)
- 闲聊轮零检索开销,思考面板显示「预召回 → 跳过(闲聊寒暄,无需检索知识库)」
- 路由事件升级:「路由 → 售后 Agent(政策咨询 · 需检索)」,意图/门控可观测
- domain 未判定时**粘性路由**(沿用上轮领域),不再瞎跳默认域
- 回滚:`query_understanding_enabled=false` 回老 Router+每轮必检索(rewrite 已并入本节点)
```

- [ ] **Step 6: 后端全量回归(recall/编排线)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_understanding.py tests/test_recall_service.py tests/test_recall_wiring.py tests/test_recall_kb.py tests/test_external_kb.py tests/test_orchestrator_qu.py tests/test_orchestrator_unified.py tests/test_controller_agent.py tests/test_emit.py tests/test_tracer.py -q`
Expected: 全部通过

- [ ] **Step 7: 浏览器端到端自测(重启后端后)**

1. 发「好的」→ 思考面板应显示「预召回 → 跳过(闲聊寒暄…)」,路由行带「免检索」,**无** ApeRAG 调用日志
2. 发「大件家电退货运费怎么算」→ 路由行「(政策咨询 · 需检索)」,预召回[ApeRAG] 命中特殊品类/物流赔付类文档
3. 上下文追问「那安装费呢」→ 预召回查询为自包含改写句
记录三轮的面板文案作为验收证据。

- [ ] **Step 8: 提交**

```bash
git add webui/src/components/AgentActivity.tsx webui/src/tests/chat-components.test.tsx docs/统一召回层-作用与效果.md
git commit -m "feat: 前端展示意图门控(route带意图/免检索徽标,recall跳过态) + 查询理解文档"
```
