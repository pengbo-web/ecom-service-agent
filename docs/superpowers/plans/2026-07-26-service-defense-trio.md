# 客服防线三件套 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按《企业级智能客服》参考文档补齐三块:①转人工触发闭环(明确要求前置短路/QU 意图直连/同一问题重复 N 次/负面情绪,打通文档 9.④ 人工兜底 SLA 整条防线);②提示词三件套(来源引用+否定约束+兜底话术,文档 5.2);③FAQ 语义缓存秒答 + 意图过滤检索(文档 2.5 缓存预热 + 2.2 三级索引的现代形态)。终验由"产品经理"子代理在前端真实体验,不满意持续迭代。

**Architecture:** ①升级判定纯函数扩展(escalation.py)+ streaming 前置短路流(`_human_request_flow`,零 LLM)+ 事后 evaluate 接入 QU 意图与重复检测;②三域提示词与 EVALUATOR 提示词文本增强;③新增 `faq_cache.py`(JSON 存储+embedding 余弦,种子来自 常见问题FAQ.md,预热直答挂在 `EcomAgent.chat` 最前)与 `kb_tags.py`(文档→领域标签,检索行按 QU domain 排序/过滤)。全部带独立开关,失败方向恒为"回退现有行为"。

**Tech Stack:** Python 3.11 + pytest;React + vitest;不新增依赖(difflib/json 标准库,Embedder 复用)。

## Global Constraints

- Python 一律 `.venv/Scripts/python.exe`;pytest `-q`;前端 `cd webui && npx vitest run` 与 `npx tsc --noEmit`;Windows gbk 不 print emoji
- 提交信息末尾:`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;不推送;`.env` 严禁提交
- 容错红线:FAQ 缓存 lookup 任何失败(embedding 挂/文件坏)返回 None 走正常流程;意图过滤 strict 模式过滤后为空必须回退全量行;升级判定纯函数不得抛出
- 前置短路红线:`match_human_fast` 只匹配 ≤10 字的**纯**转人工请求(整句锚定),"人工审核要多久"这类正常问题绝不短路
- 测试环境红线:`tests/conftest.py` 钉 `faq_cache_enabled=False`(lookup 会调 embedding,不钉则全套测试碰网络);FAQ 相关新测试自行开 True 并 mock embedding
- 既有接口零破坏:`should_escalate`/`evaluate` 新参数全部带默认值;`build_recall_sections`/`kb_recall` 新参数带默认 None;现有调用不改行为
- 事件契约(前端依赖):新增 `{"type":"faq_cache","matched":<命中问题>,"score":<float>}`;handoff 事件形态不变
- 工作目录:`D:\2026项目\ecom-service-agent`

---

### Task 1: 升级判定纯函数扩展(escalation.py)

**Files:**
- Modify: `app/hitl/escalation.py`
- Modify: `app/hitl/manager.py`(evaluate 透传新参数)
- Modify: `app/config/settings.py`(`hitl_repeat_times` 一项,插在 `manual_mode_timeout` 之后)
- Test: `tests/test_escalation_loop.py`(新)

**Interfaces:**
- Produces: `match_human_fast(text: str) -> bool`(前置短路判定);`repeated_unresolved(user_input: str, prior_user_msgs: list, times: int = 3, sim: float = 0.75) -> bool`;`should_escalate(..., qu_intent: str = "", prior_user_msgs: list | None = None, repeat_times: int = 3) -> list`;`HitlManager.evaluate(..., qu_intent: str = "", prior_user_msgs: list | None = None) -> list`。Task 2 的 streaming 消费全部四个。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_escalation_loop.py`:

```python
"""转人工闭环判定:前置短路/重复无解/QU意图直连/负面情绪。"""

from app.hitl.escalation import (match_human_fast, repeated_unresolved,
                                 should_escalate)
from app.hitl.manager import HitlManager


# ---- 前置短路:只吃纯转人工短句 ----

def test_match_human_fast_pure_requests():
    for t in ["转人工", "人工客服", "找人工", "真人客服", "我要人工", "转人工!"]:
        assert match_human_fast(t) is True, t


def test_match_human_fast_never_eats_normal_questions():
    for t in ["人工审核要多久", "人工客服几点上班", "转人工之前先帮我查下订单",
              "退货要人工审核吗", ""]:
        assert match_human_fast(t) is False, t


# ---- 同一问题重复 N 次 ----

def test_repeated_three_times_triggers():
    prior = ["退款怎么还没到账", "退款怎么还没到啊", "我问商品推荐"]
    assert repeated_unresolved("退款怎么还没到账?", prior, times=3) is True


def test_two_times_not_enough():
    prior = ["退款怎么还没到账"]
    assert repeated_unresolved("退款怎么还没到账?", prior, times=3) is False


def test_short_ack_never_counts_as_repeat():
    prior = ["好的", "好的"]
    assert repeated_unresolved("好的", prior, times=3) is False


def test_different_questions_not_repeat():
    prior = ["退货运费谁承担", "价保期限多久"]
    assert repeated_unresolved("发票抬头怎么改", prior, times=3) is False


# ---- should_escalate 新增原因 ----

def _base(**kw):
    args = dict(intent="other", confidence=0.9, requires_human=False,
                threshold=0.6, sensitive_intents={"complaint"}, user_input="")
    args.update(kw)
    return should_escalate(**args)


def test_qu_intent_human_request_direct():
    reasons = _base(qu_intent="转人工")
    assert any("明确要求转人工" in r for r in reasons)


def test_qu_intent_complaint_direct():
    reasons = _base(qu_intent="投诉")
    assert any("投诉倾向" in r for r in reasons)


def test_repeat_reason_included():
    reasons = _base(user_input="退款怎么还没到账?",
                    prior_user_msgs=["退款怎么还没到账", "退款怎么还没到啊"])
    assert any("重复" in r for r in reasons)


def test_anger_keywords_escalate():
    reasons = _base(user_input="你们就是骗子,垃圾平台")
    assert any("负面情绪" in r for r in reasons)


def test_no_new_reasons_on_calm_normal_turn():
    assert _base(user_input="退货运费谁承担",
                 prior_user_msgs=["之前问的是价保"]) == []


def test_manager_passthrough():
    m = HitlManager(confidence_threshold=0.6)
    reasons = m.evaluate("other", 0.9, False, user_input="随便问问",
                         qu_intent="转人工", prior_user_msgs=[])
    assert any("明确要求转人工" in r for r in reasons)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_escalation_loop.py -q`
Expected: FAIL(ImportError: match_human_fast / TypeError: unexpected keyword qu_intent)

- [ ] **Step 3: 加配置**

`app/config/settings.py` 在 `manual_mode_timeout` 行之后插入:

```python
    hitl_repeat_times: int = 3        # 同一问题重复 N 次未解决→自动转人工(文档9.④,difflib相似度判同)
```

- [ ] **Step 4: 写实现**

`app/hitl/escalation.py` 顶部 import 区加 `import re` 与 `import difflib`;在 `ESCALATION_KEYWORDS` 之后追加:

```python
# 明确要求人工的纯短句(前置短路用:整句锚定+长度上限,绝不误伤"人工审核要多久"这类正常问题)
HUMAN_REQUEST_PATTERNS = [
    re.compile(r"^(转人工|人工客服|找人工|叫?真人客服?|我要人工|人工服务)[\s!！~。.,，]*$"),
]

# 负面情绪词(文档5.2情绪识别前置/9.④):命中即升级转人工,回复照常给(安抚+banner,不短路)
ANGER_KEYWORDS = ["垃圾", "骗子", "气死", "太差劲", "什么破", "忽悠", "糊弄", "敷衍", "废物", "投诉你们"]


def match_human_fast(text: str) -> bool:
    """纯转人工请求判定(≤10字整句命中)——streaming 前置短路用,零 LLM 直接转接。"""
    t = (text or "").strip()
    if not t or len(t) > 10:
        return False
    return any(p.match(t) for p in HUMAN_REQUEST_PATTERNS)


def repeated_unresolved(user_input: str, prior_user_msgs: list,
                        times: int = 3, sim: float = 0.75) -> bool:
    """同一问题重复 times 次(含本次)判定:与既往用户消息相似度>=sim 的条数达 times-1。
    短语气词(<4字)不算问题,避免"好的好的"误判。"""
    t = (user_input or "").strip()
    if len(t) < 4:
        return False
    similar = 0
    for m in (prior_user_msgs or []):
        m = (m or "").strip()
        if len(m) < 4:
            continue
        if difflib.SequenceMatcher(None, t, m).ratio() >= sim:
            similar += 1
    return similar >= times - 1
```

`should_escalate` 签名与函数体扩展(新参数带默认,既有调用零改动):

```python
def should_escalate(intent: str, confidence: float, requires_human: bool,
                    threshold: float, sensitive_intents: set,
                    user_input: str = "", qu_intent: str = "",
                    prior_user_msgs: list | None = None,
                    repeat_times: int = 3) -> list:
    """返回命中的升级原因列表；空列表表示无需转人工。"""
    reasons = []
    if requires_human:
        reasons.append("模型判定需转人工")
    if confidence < threshold:
        reasons.append(f"置信度过低({confidence:.2f} < {threshold})")
    if intent in sensitive_intents:
        reasons.append(f"敏感意图({intent})")
    hit = [k for k in ESCALATION_KEYWORDS if k in (user_input or "")]
    if hit:
        reasons.append(f"命中升级关键词({'、'.join(hit)})")
    # —— 转人工闭环(文档9.④)——
    if qu_intent == "转人工":
        reasons.append("用户明确要求转人工(查询理解)")
    elif qu_intent == "投诉":
        reasons.append("投诉倾向(查询理解)")
    if prior_user_msgs and repeated_unresolved(user_input, prior_user_msgs,
                                               times=repeat_times):
        reasons.append(f"同一问题重复{repeat_times}次未解决")
    anger = [k for k in ANGER_KEYWORDS if k in (user_input or "")]
    if anger:
        reasons.append(f"负面情绪({'、'.join(anger[:3])})")
    return reasons
```

(注意:现有函数体里"命中升级关键词"那段的实际文案以文件现状为准,保留原样,只在其后追加新块。)

`app/hitl/manager.py` 的 `evaluate` 改为:

```python
    def evaluate(self, intent: str, confidence: float, requires_human: bool,
                 user_input: str = "", qu_intent: str = "",
                 prior_user_msgs: list | None = None) -> list:
        from app.config.settings import settings
        return should_escalate(intent, confidence, requires_human,
                               self.confidence_threshold, self.sensitive_intents,
                               user_input=user_input, qu_intent=qu_intent,
                               prior_user_msgs=prior_user_msgs,
                               repeat_times=settings.hitl_repeat_times)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_escalation_loop.py -q`
Expected: 12 passed
Run: `.venv/Scripts/python.exe -m pytest tests/ -q -k "hitl or escalat or handoff" 2>&1 | tail -3`
Expected: 既有 HITL 相关测试全绿(新参数带默认,零破坏)

- [ ] **Step 6: 提交**

```bash
git add app/hitl/escalation.py app/hitl/manager.py app/config/settings.py tests/test_escalation_loop.py
git commit -m "feat: 升级判定扩展——转人工纯短句/重复N次无解/QU意图直连/负面情绪(文档9.④)"
```

---

### Task 2: streaming 接线(前置短路 + 事后升级带 QU/重复)

**Files:**
- Modify: `app/api/streaming.py`(新增 `_human_request_flow`;`_normal_flow` 的 evaluate 调用扩参;流选择分支)
- Modify: `app/multi_agent/orchestrator.py`(只读属性 `_turn_qu` 委托给引擎,模仿既有 `_pending` 委托风格)
- Test: `tests/test_streaming_handoff.py`(新)

**Interfaces:**
- Consumes: Task 1 的 `match_human_fast`、`evaluate(..., qu_intent=, prior_user_msgs=)`;引擎 `_turn_qu`(QueryUnderstanding 或 None)
- Produces: 前置短路流:用户纯转人工请求 → 不调任何 LLM,发 handoff+reply+metadata 三事件,固定话术转接

- [ ] **Step 1: 写失败测试**

创建 `tests/test_streaming_handoff.py`:

```python
"""转人工闭环 streaming 层:前置短路零LLM/事后升级带QU意图与重复检测。"""

from types import SimpleNamespace

from app.api.streaming import run_agent_streaming
from app.hitl.manager import HitlManager


class FakeAgent:
    """chat 若被调用则记录——前置短路场景中它绝不应被调用。"""
    def __init__(self):
        self.raw_messages = []
        self.user_id = "u1"
        self.chat_called = 0
        self._turn_qu = None
        self._pending = None

    def chat(self, user_input):
        self.chat_called += 1
        return SimpleNamespace(
            reply="正常回答", intent=SimpleNamespace(value="other"),
            confidence=0.9, requires_human=False, follow_up_question=None,
            model_dump_json=lambda: "{}")


def _drain(agent, text, hitl):
    return list(run_agent_streaming(agent, text, session_id="s1", hitl=hitl))


def test_pure_human_request_short_circuits_without_llm(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.memory.profile.record_ticket", lambda *a, **k: None)
    hitl = HitlManager(confidence_threshold=0.6)
    agent = FakeAgent()
    events = _drain(agent, "转人工", hitl)
    assert agent.chat_called == 0                     # 零 LLM
    types = [e["type"] for e in events]
    assert "handoff" in types and "reply" in types and "metadata" in types
    handoff = [e for e in events if e["type"] == "handoff"][0]
    assert any("明确要求转人工" in r for r in handoff["reasons"])
    reply = [e for e in events if e["type"] == "reply"][0]
    assert "人工" in reply["content"]


def test_normal_question_not_short_circuited(monkeypatch):
    monkeypatch.setattr("app.agent.memory.profile.record_ticket", lambda *a, **k: None)
    hitl = HitlManager(confidence_threshold=0.6)
    agent = FakeAgent()
    _drain(agent, "人工审核要多久", hitl)
    assert agent.chat_called == 1                     # 正常走 Agent


def test_post_evaluate_receives_qu_intent(monkeypatch):
    monkeypatch.setattr("app.agent.memory.profile.record_ticket", lambda *a, **k: None)
    captured = {}
    hitl = HitlManager(confidence_threshold=0.1)      # 低阈值避免误升
    orig = hitl.evaluate
    def spy(*a, **k):
        captured.update(k)
        return []
    hitl.evaluate = spy
    agent = FakeAgent()
    agent._turn_qu = SimpleNamespace(intent="投诉", need_kb=False,
                                     domain=None, kb_query=None, source="llm")
    agent.raw_messages = [{"role": "user", "content": "上一条"},
                          {"role": "user", "content": "本轮消息"}]
    _drain(agent, "本轮消息", hitl)
    assert captured.get("qu_intent") == "投诉"
    assert captured.get("prior_user_msgs") == ["上一条"]   # 不含本轮
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_streaming_handoff.py -q`
Expected: FAIL(短路流不存在,chat_called==1;evaluate 无 qu_intent 参数)

- [ ] **Step 3: 改 streaming.py**

3a. 在 `_normal_flow` 定义之前(与 `_blocked_flow` 平级)新增:

```python
    def _human_request_flow(_sink) -> str:
        """明确要求转人工:前置短路,零 LLM(文档9.④强制转人工词+8.3'必须明确提示正在转接')。"""
        reply = "好的,正在为您转接人工客服,请稍候~ 转接期间您可以继续补充问题,人工客服会看到完整对话记录。"
        recent = list(getattr(agent, "raw_messages", []))[-6:]
        hid = hitl.escalate(session_id, user_input, reply, "human_request", 1.0,
                            ["用户明确要求转人工"], recent_context=recent)
        from app.agent.memory.profile import record_ticket
        record_ticket(getattr(agent, "user_id", None), hid, "escalated",
                      "用户明确要求转人工")
        _sink({"type": "handoff", "reasons": ["用户明确要求转人工"], "handoff_id": hid})
        _sink({"type": "reply", "content": reply})
        _sink({"type": "metadata", "intent": "human_request", "confidence": 1.0,
               "requires_human": True, "follow_up_question": None})
        return "human_request"
```

3b. 找到流选择处(输入护栏判定之后、进入 `_normal_flow`/pending 重放之前的分支逻辑),插入前置短路分支:

```python
        from app.hitl.escalation import match_human_fast
        if hitl is not None and match_human_fast(user_input):
            return _human_request_flow(_sink)
```

(放置原则:必须在输入护栏 block 判定之后——被护栏拦截的消息仍走 `_blocked_flow`;在正常 Agent 流之前。)

3c. `_normal_flow` 中 `hitl.evaluate(...)` 调用扩参:

```python
            qu = getattr(agent, "_turn_qu", None)
            all_user = [m.get("content", "") for m in getattr(agent, "raw_messages", [])
                        if m.get("role") == "user"]
            prior_user = all_user[:-1] if all_user else []   # 排除本轮
            reasons = hitl.evaluate(result.intent.value, result.confidence,
                                    result.requires_human, user_input=user_input,
                                    qu_intent=(qu.intent if qu is not None else ""),
                                    prior_user_msgs=prior_user)
```

- [ ] **Step 4: 改 orchestrator.py(属性委托)**

在既有 `_pending` property 附近追加(只读):

```python
    @property
    def _turn_qu(self):                      # 查询理解结果透传(streaming 升级判定读)
        return self.engine._turn_qu
```

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_streaming_handoff.py tests/test_escalation_loop.py tests/test_streaming_trace.py -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/api/streaming.py app/multi_agent/orchestrator.py tests/test_streaming_handoff.py
git commit -m "feat: 转人工触发闭环——前置短路零LLM + 事后升级接QU意图/重复检测"
```

---

### Task 3: 提示词三件套 + EVALUATOR 来源真实性

**Files:**
- Modify: `app/prompts/agents.py`(三域规则尾部统一追加两条,replace_all)
- Modify: `app/prompts/reply_pipeline.py`(EVALUATOR 接地维度补来源校验)
- Test: `tests/test_prompt_constraints.py`(新)

**Interfaces:** 无代码接口(纯提示词);来源标注格式约定「(依据《文档名》)」,Task 5 的 FAQ 直答复用同格式。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_prompt_constraints.py`:

```python
"""提示词三件套:来源引用/否定约束/兜底话术 + EVALUATOR 来源真实性。"""

from app.prompts.agents import AFTERSALE_PROMPT, MIDSALE_PROMPT, PRESALE_PROMPT
from app.prompts.reply_pipeline import EVALUATOR_PROMPT

ALL_DOMAIN = [PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT]


def test_source_citation_rule_in_all_domains():
    for p in ALL_DOMAIN:
        assert "依据《" in p and "严禁编造来源" in p


def test_negative_constraint_in_all_domains():
    for p in ALL_DOMAIN:
        assert "我猜" in p and "应该是" in p


def test_fallback_phrase_in_all_domains():
    for p in ALL_DOMAIN:
        assert "进一步核实" in p and "requires_human" in p


def test_evaluator_checks_citation_authenticity():
    assert "来源" in EVALUATOR_PROMPT and "编造来源" in EVALUATOR_PROMPT
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompt_constraints.py -q`
Expected: 4 failed

- [ ] **Step 3: 改三域提示词(replace_all,三处相同)**

`app/prompts/agents.py` 中三处相同的规则尾巴:

```
未覆盖时仍须检索"""
```

全部替换为:

```
未覆盖时仍须检索
8. 回答政策/规则问题时在相应结论句末标注来源,格式「(依据《文档名》)」——文档名只能取自本轮【平台知识(自动检索)】段或 search_knowledge 结果里的真实 doc 名,**严禁编造来源**;闲聊与纯订单操作回复不标注
9. **严禁使用「我猜」「可能是」「应该是」「大概」等不确定表述**——不确定就先检索;当检索与工具都无法回答时,使用统一兜底话术:「这个问题我需要进一步核实,已为您记录,建议转人工客服获取准确答复」,并如实设置 requires_human=true"""
```

(注:presale 域该规则编号是 7,mid/after 是 6——追加的两条在各域内顺延为下两号,replace_all 后编号统一写 8/9 即可,三域规则数不同不影响模型理解;实现时保持上述文本原样即可。)

- [ ] **Step 4: 改 EVALUATOR 提示词**

`app/prompts/reply_pipeline.py` 的 EVALUATOR_PROMPT 接地维度(第 1 条,以「判不合格。」结尾的那段)末尾追加一句:

```
草稿中「(依据《…》)」引用的来源名必须真实出现在本轮工具结果或【平台知识(自动检索)】段中,引用了不存在的来源=编造来源,判不合格。
```

- [ ] **Step 5: 跑测试确认通过 + 模块可导入**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompt_constraints.py tests/test_policy_grounding.py -q 2>&1 | tail -2`
(若 tests/test_policy_grounding.py 不存在则跑 `tests/test_prompt_constraints.py` 加 `-k prompt` 的相关既有测试)
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/prompts/agents.py app/prompts/reply_pipeline.py tests/test_prompt_constraints.py
git commit -m "feat: 提示词三件套——来源引用+否定约束+兜底话术,EVALUATOR校验来源真实性(文档5.2)"
```

---

### Task 4: FAQ 语义缓存模块 + 预热种子

**Files:**
- Create: `app/agent/faq_cache.py`
- Create: `scripts/build_faq_cache.py`
- Modify: `app/config/settings.py`(3 项,插在 `hitl_repeat_times` 之后)
- Modify: `tests/conftest.py`(钉 `faq_cache_enabled=False`,既有风格)
- Test: `tests/test_faq_cache.py`(新)

**Interfaces:**
- Consumes: `app.agent.rag.embedder.Embedder(api_key, base_url, model, timeout, max_retries)` 的 `encode_one(text) -> list[float]`
- Produces: `get_faq_cache() -> FaqCache`(进程单例,`reset_faq_cache()` 供测试);`FaqCache.lookup(query) -> dict | None`(命中返回 `{"question","answer","score"}`);`FaqCache.add(question, answer)`。Task 5 的 chat 钩子消费 lookup。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_faq_cache.py`:

```python
"""FAQ 语义缓存:命中/阈值/容错/单例。"""

import app.agent.faq_cache as fc
from app.agent.faq_cache import FaqCache
from app.config.settings import settings


def _cache(tmp_path, monkeypatch, entries=None):
    monkeypatch.setattr(settings, "faq_cache_enabled", True)
    c = FaqCache(str(tmp_path / "faq.json"))
    # 打桩 embedding:确定性向量,免网络
    vecs = {"下单后多久发货": [1.0, 0.0], "下单多久能发货?": [0.98, 0.199],
            "价保多久": [0.0, 1.0], "完全无关的问题": [0.7, 0.714]}
    monkeypatch.setattr(c, "_embed", lambda t: vecs.get(t, [0.5, 0.5]))
    for q, a in (entries or []):
        c.add(q, a)
    return c


def test_hit_above_threshold(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    hit = c.lookup("下单多久能发货?")
    assert hit is not None and hit["answer"] == "48小时内出库"
    assert hit["question"] == "下单后多久发货" and hit["score"] >= 0.9


def test_below_threshold_misses(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    assert c.lookup("价保多久") is None


def test_disabled_returns_none(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    monkeypatch.setattr(settings, "faq_cache_enabled", False)
    assert c.lookup("下单多久能发货?") is None


def test_embed_failure_returns_none(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    def boom(t):
        raise RuntimeError("embedding down")
    monkeypatch.setattr(c, "_embed", boom)
    assert c.lookup("下单多久能发货?") is None      # 容错:失败=未命中


def test_persistence_roundtrip(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    c2 = FaqCache(str(tmp_path / "faq.json"))       # 重新加载文件
    monkeypatch.setattr(c2, "_embed", lambda t: [0.98, 0.199])
    assert c2.lookup("下单多久能发货?")["answer"] == "48小时内出库"


def test_corrupt_file_tolerated(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    c = FaqCache(str(p))                             # 不抛,空缓存
    assert c.entries == []


def test_singleton_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "faq_cache_path", str(tmp_path / "s.json"))
    fc.reset_faq_cache()
    a = fc.get_faq_cache()
    assert a is fc.get_faq_cache()
    fc.reset_faq_cache()
    assert a is not fc.get_faq_cache()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_faq_cache.py -q`
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 3: 加配置与 conftest 钉桩**

`app/config/settings.py` 在 `hitl_repeat_times` 行之后插入:

```python
    # FAQ 语义缓存(文档2.5缓存预热):高频问答预热直答,命中零LLM;种子来自常见问题FAQ.md
    faq_cache_enabled: bool = True
    faq_cache_path: str = "app/sessions/faq_cache.json"
    faq_cache_min_score: float = 0.90   # 余弦阈值:高置信才直答,答错比答慢更伤信任
```

`tests/conftest.py` autouse fixture 按既有风格钉 `settings.faq_cache_enabled = False` 并 teardown 还原。

- [ ] **Step 4: 写实现**

创建 `app/agent/faq_cache.py`:

```python
"""FAQ 语义缓存:高频问答预热直答(对齐《企业级智能客服》2.5 缓存预热,命中率73%)。

存储:JSON 文件 {"entries": [{"q": 问题, "a": 答案, "emb": [向量]}]};
种子:scripts/build_faq_cache.py 离线解析 常见问题FAQ.md 的 Q/A 对预热入库。
服务:lookup(query) 用与 KB 相同的 embedding 模型算余弦,>=阈值即命中直答——
政策类 FAQ 与用户无关,可安全跨用户复用;订单/账户类问题由 QU 门控挡在缓存外。

容错红线:embedding 失败/文件损坏一律当未命中,回退正常 Agent 流程。
"""

import json
import logging
import math
from pathlib import Path

from app.config.settings import settings

logger = logging.getLogger(__name__)

_singleton = None


def _cos(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class FaqCache:
    def __init__(self, path: str):
        self.path = Path(path)
        self.entries: list[dict] = []
        self._embedder = None
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.entries = data.get("entries", [])
        except Exception:
            logger.warning("faq cache load failed, start empty", exc_info=True)
            self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"entries": self.entries}, ensure_ascii=False),
                             encoding="utf-8")

    def _embed(self, text: str) -> list[float]:
        if self._embedder is None:
            from app.agent.rag.embedder import Embedder
            self._embedder = Embedder(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.embedding_model,
                timeout=settings.recall_kb_timeout_s,
                max_retries=settings.recall_kb_embed_retries,
            )
        return self._embedder.encode_one(text)

    def add(self, question: str, answer: str) -> None:
        self.entries.append({"q": question, "a": answer,
                             "emb": self._embed(question)})
        self._save()

    def lookup(self, query: str):
        """命中返回 {"question","answer","score"};未命中/关闭/失败返回 None。"""
        if not settings.faq_cache_enabled or not query or not self.entries:
            return None
        try:
            q_vec = self._embed(query)
        except Exception:
            logger.warning("faq cache embed failed, treat as miss", exc_info=True)
            return None
        best, best_score = None, 0.0
        for e in self.entries:
            s = _cos(q_vec, e.get("emb") or [])
            if s > best_score:
                best, best_score = e, s
        if best is not None and best_score >= settings.faq_cache_min_score:
            return {"question": best["q"], "answer": best["a"],
                    "score": round(best_score, 4)}
        return None


def get_faq_cache() -> FaqCache:
    global _singleton
    if _singleton is None:
        _singleton = FaqCache(settings.faq_cache_path)
    return _singleton


def reset_faq_cache() -> None:
    global _singleton
    _singleton = None
```

- [ ] **Step 5: 写种子脚本**

创建 `scripts/build_faq_cache.py`:

```python
"""FAQ 缓存预热:解析 常见问题FAQ.md 的 Q/A 对,embedding 后写入缓存文件。

用法: .venv/Scripts/python.exe scripts/build_faq_cache.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.faq_cache import FaqCache
from app.config.settings import settings


def parse_faq(md_text: str) -> list[tuple[str, str]]:
    """### Qn：问题\n答案(至下一个 #) → (问题, 答案) 对。"""
    pairs = []
    blocks = re.split(r"^### Q\d+[：:]", md_text, flags=re.M)[1:]
    for b in blocks:
        lines = b.strip().splitlines()
        if not lines:
            continue
        q = lines[0].strip()
        body = []
        for ln in lines[1:]:
            if ln.startswith("#"):
                break
            body.append(ln)
        a = "\n".join(body).strip()
        if q and a:
            pairs.append((q, a))
    return pairs


def main() -> int:
    md = Path("app/agent/rag/knowledge/常见问题FAQ.md").read_text(encoding="utf-8")
    pairs = parse_faq(md)
    cache = FaqCache(settings.faq_cache_path)
    cache.entries = []                      # 重建:幂等
    for q, a in pairs:
        cache.add(q, a)
        print("cached:", q)
    print(f"total {len(pairs)} entries -> {settings.faq_cache_path}")
    return 0 if pairs else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: 跑测试 + 实跑种子**

Run: `.venv/Scripts/python.exe -m pytest tests/test_faq_cache.py -q`
Expected: 7 passed
Run: `.venv/Scripts/python.exe -X utf8 scripts/build_faq_cache.py`
Expected: 输出 13 条 cached + total 13(真实 embedding,一次性;faq_cache.json 在 app/sessions/ 下,gitignored 不提交)

- [ ] **Step 7: 提交**

```bash
git add app/agent/faq_cache.py scripts/build_faq_cache.py app/config/settings.py tests/conftest.py tests/test_faq_cache.py
git commit -m "feat: FAQ 语义缓存模块+预热种子脚本(文档2.5缓存预热,13条Q/A入库)"
```

---

### Task 5: FAQ 秒答接线(chat 钩子 + 前端事件)

**Files:**
- Modify: `app/agent/chat.py`(`chat()` 在 react 之前插入缓存短路)
- Modify: `webui/src/components/ChatView.tsx`(白名单加 "faq_cache")
- Modify: `webui/src/components/AgentActivity.tsx`(faq_cache 行,Zap 图标)
- Test: `tests/test_faq_serving.py`(新)+ `webui/src/tests/chat-components.test.tsx`(追加)

**Interfaces:**
- Consumes: Task 4 `get_faq_cache().lookup(query)`;引擎 `_turn_qu`(need_kb/kb_query)
- Produces: 事件 `{"type":"faq_cache","matched":<命中问题>,"score":<float>}`;命中轮直接返回 `CustomerServiceResponse(intent=OTHER, confidence=1.0, reply=答案+"\n(依据《常见问题FAQ》)")`,零 LLM,会话历史/落盘照常

- [ ] **Step 1: 写失败测试**

创建 `tests/test_faq_serving.py`:

```python
"""FAQ 秒答接线:命中短路零LLM/未命中正常流/门控外问题不查缓存。"""

from types import SimpleNamespace

from app.agent.chat import EcomAgent
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings


def _agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "faq_cache_enabled", True)
    agent = EcomAgent(session_path=str(tmp_path / "s.json"))
    agent.event_sink = agent_events.append
    monkeypatch.setattr(agent, "_write_snapshot", lambda: None)   # 隔离:不落真实快照库
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: (_ for _ in ()).throw(AssertionError("LLM 不应被调用")))
    return agent


agent_events = []


def test_hit_short_circuits_llm(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr("app.agent.faq_cache.get_faq_cache",
                        lambda: SimpleNamespace(lookup=lambda q: {
                            "question": "下单后多久发货", "answer": "48小时内出库",
                            "score": 0.95}))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="下单多久发货", source="llm"))
    result = agent.chat("下单多久能发货?")
    assert "48小时内出库" in result.reply and "依据《常见问题FAQ》" in result.reply
    assert result.confidence == 1.0
    ev = [e for e in agent_events if e["type"] == "faq_cache"][0]
    assert ev["matched"] == "下单后多久发货" and ev["score"] == 0.95
    # 会话历史照常落账:user + assistant 两条
    assert agent.raw_messages[-2]["role"] == "user"
    assert agent.raw_messages[-1]["role"] == "assistant"


def test_need_kb_false_skips_cache(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr("app.agent.faq_cache.get_faq_cache",
                        lambda: SimpleNamespace(lookup=lambda q: called.append(q)))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="闲聊寒暄", need_kb=False, source="rule"))
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}')
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())
    agent.chat("好的")
    assert called == []                              # 免检索轮不查缓存


def test_miss_falls_through_to_agent(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr("app.agent.faq_cache.get_faq_cache",
                        lambda: SimpleNamespace(lookup=lambda q: None))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="冷门问题", source="llm"))
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: '{"intent":"other","confidence":0.9,"reply":"正常回答","requires_human":false}')
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())
    result = agent.chat("冷门问题")
    assert "正常回答" in result.reply
    assert [e for e in agent_events if e["type"] == "faq_cache"] == []
```

(注:test 中 build_recall_sections 的桩带 `kb_domain=None` 参数——Task 6 会加该参数;若 Task 5 先行,该参数暂不存在也不影响,桩多收参数是向前兼容。)

`webui/src/tests/chat-components.test.tsx` 追加:

```tsx
describe("AgentActivity FAQ 秒答", () => {
  it("渲染缓存命中事件", () => {
    render(<AgentActivity events={[
      { type: "faq_cache", matched: "下单后多久发货", score: 0.95 },
    ]} defaultOpen />);
    expect(screen.getByText(/FAQ 秒答/)).toBeInTheDocument();
    expect(screen.getByText(/下单后多久发货/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_faq_serving.py -q`
Expected: FAIL(命中场景 AssertionError "LLM 不应被调用" / faq_cache 事件不存在)

- [ ] **Step 3: 改 chat.py**

`chat()` 中 `self._checkpoint("in_flight")` 行之后、`stage react` 事件之前插入:

```python
        # FAQ 语义缓存秒答(文档2.5缓存预热):QU 判定需检索的政策类问题先查预热缓存,
        # 命中=零 LLM 直答(毫秒级);未命中/关闭/失败走正常流程。会话落账与持久化照常。
        if (settings.faq_cache_enabled and self._turn_qu is not None
                and self._turn_qu.need_kb):
            from app.agent.faq_cache import get_faq_cache
            _hit = get_faq_cache().lookup(self._turn_qu.kb_query or user_input)
            if _hit is not None:
                self._emit({"type": "faq_cache", "matched": _hit["question"],
                            "score": _hit["score"]})
                result = CustomerServiceResponse(
                    intent=IntentType.OTHER, confidence=1.0,
                    reply=_hit["answer"] + "\n(依据《常见问题FAQ》)",
                    requires_human=False, follow_up_question=None)
                self.raw_messages.append(
                    {"role": "assistant", "content": result.model_dump_json()})
                self._status = "complete"
                self.store.save(self.session_path, self._session_state())
                self._write_snapshot()
                return result
```

- [ ] **Step 4: 改前端**

`webui/src/components/ChatView.tsx` 事件白名单数组追加 `"faq_cache"`。
`webui/src/components/AgentActivity.tsx` import 加 `Zap`;recall 两分支之后追加:

```tsx
              {e.type === "faq_cache" && <><Zap className="mt-0.5 h-3.5 w-3.5 text-accent" /><span>FAQ 秒答（命中：{e.matched}，相似度 {Number(e.score).toFixed(2)}，零 LLM）</span></>}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_faq_serving.py tests/test_faq_cache.py -q`
Expected: 全部通过
Run: `cd webui && npx vitest run && npx tsc --noEmit`
Expected: 全绿,tsc 无输出

- [ ] **Step 6: 提交**

```bash
git add app/agent/chat.py webui/src/components/ChatView.tsx webui/src/components/AgentActivity.tsx tests/test_faq_serving.py webui/src/tests/chat-components.test.tsx
git commit -m "feat: FAQ 秒答接线——政策问题命中缓存零LLM直答,前端Zap事件可见"
```

---

### Task 6: 意图过滤检索(三级索引现代形态)

**Files:**
- Create: `app/agent/recall/kb_tags.py`
- Modify: `app/agent/recall/kb.py`(`kb_recall(query, domain=None)`,取行后按域排序/过滤)
- Modify: `app/agent/recall/service.py`(`build_recall_sections(..., kb_domain=None)` 透传)
- Modify: `app/agent/chat.py`(传 `kb_domain=qu.domain`)
- Modify: `app/multi_agent/orchestrator.py`(粘性解析后回填 `qu.domain = key`)
- Modify: `app/config/settings.py`(`recall_domain_mode` 一项,插在 `faq_cache_min_score` 之后)
- Test: `tests/test_kb_domain_filter.py`(新)+ `tests/test_recall_wiring.py`(追加 1 例)

**Interfaces:**
- Consumes: QU 的 `domain`(orchestrator 已解析粘性后回填,不再为 None)
- Produces: `rank_by_domain(rows: list[dict], domain: str | None) -> list[dict]`(mode=off 原样;boost 匹配域行稳定前置;strict 只留匹配行,**空则回退全量**);`kb_recall(query, domain=None)`;`build_recall_sections(memory_manager, query, include_kb=True, kb_domain=None)`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_kb_domain_filter.py`:

```python
"""意图过滤检索:boost 前置/strict 过滤带回退/off 原样/未知文档不误杀。"""

import app.agent.recall.kb as kb_mod
from app.agent.recall.kb import kb_recall
from app.agent.recall.kb_tags import rank_by_domain
from app.config.settings import settings

ROWS = [
    {"doc": "优惠券与促销规则.md", "section": "s", "score": 0.9, "text": "券"},
    {"doc": "退换货政策.md", "section": "s", "score": 0.8, "text": "退"},
    {"doc": "神秘新文档.md", "section": "s", "score": 0.7, "text": "未知"},
]


def test_boost_moves_matching_first(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "boost")
    out = rank_by_domain(list(ROWS), "aftersale")
    assert out[0]["doc"] == "退换货政策.md"           # 匹配域前置
    assert len(out) == 3                              # 不丢行
    assert out[1]["doc"] == "优惠券与促销规则.md"      # 其余保持原相对顺序


def test_strict_filters_but_falls_back_when_empty(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "strict")
    out = rank_by_domain(list(ROWS), "aftersale")
    docs = [r["doc"] for r in out]
    assert "退换货政策.md" in docs and "优惠券与促销规则.md" not in docs
    assert "神秘新文档.md" in docs                     # 未知标签文档不误杀
    only_presale = [{"doc": "退换货政策.md", "section": "s", "score": 0.8, "text": "退"}]
    out2 = rank_by_domain(only_presale, "presale")
    assert out2 == only_presale                        # 过滤为空→回退全量


def test_off_mode_and_none_domain_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "off")
    assert rank_by_domain(list(ROWS), "aftersale") == ROWS
    monkeypatch.setattr(settings, "recall_domain_mode", "boost")
    assert rank_by_domain(list(ROWS), None) == ROWS


def test_kb_recall_applies_domain(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "boost")
    monkeypatch.setattr(kb_mod, "_fetch_rows",
                        lambda q: (list(ROWS), "local"))
    monkeypatch.setattr(settings, "recall_kb_min_score", 0.0)
    r = kb_recall("退货", domain="aftersale")
    assert r.hits[0]["doc"] == "退换货政策.md"
```

`tests/test_recall_wiring.py` 追加:

```python
def test_chat_passes_qu_domain_to_recall(monkeypatch):
    from app.agent.understanding import QueryUnderstanding
    captured = {}

    def fake_recall(mm, query, include_kb=True, kb_domain=None):
        captured.update(query=query, kb_domain=kb_domain)
        return RecallResult()

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    agent.event_sink = lambda e: None
    agent.set_turn_understanding(QueryUnderstanding(
        domain="aftersale", intent="政策咨询", need_kb=True,
        kb_query="退货运费", source="llm"))
    agent.raw_messages.append({"role": "user", "content": "运费"})
    agent._turn_recall = None
    agent._build_messages()
    assert captured["kb_domain"] == "aftersale"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_kb_domain_filter.py tests/test_recall_wiring.py -q`
Expected: FAIL(kb_tags 不存在 / kb_domain 参数不存在)

- [ ] **Step 3: 加配置**

`app/config/settings.py` 在 `faq_cache_min_score` 行之后插入:

```python
    # 意图过滤检索(文档2.2三级索引):检索行按 QU domain 排序/过滤
    # off=不动 | boost(默认)=匹配域稳定前置,不丢行 | strict=只留匹配域,空则回退全量
    recall_domain_mode: str = "boost"
```

- [ ] **Step 4: 写 kb_tags.py**

```python
"""文档→领域标签:意图过滤检索的数据面(对齐《企业级智能客服》2.2 三级索引一二级)。

标签是"该文档主要服务哪些领域"的软标注:boost 模式只影响排序绝不丢行;
strict 模式过滤但空结果回退全量;未收录的文档视为全域(宁多勿漏)。
"""

from app.config.settings import settings

DOC_DOMAIN_TAGS: dict[str, set] = {
    "退换货政策": {"aftersale"},
    "售后维修与三包": {"aftersale"},
    "投诉与纠纷处理": {"aftersale"},
    "账户与安全": {"aftersale"},
    "物流异常与赔付标准": {"midsale", "aftersale"},
    "发票与支付说明": {"midsale", "aftersale"},
    "订单管理规则": {"midsale"},
    "配送说明": {"presale", "midsale"},
    "优惠券与促销规则": {"presale"},
    "价格保护政策": {"presale", "aftersale"},
    "会员权益": {"presale", "aftersale"},
    "特殊品类服务规则": {"presale", "aftersale"},
    "常见问题FAQ": {"presale", "midsale", "aftersale"},
}


def _doc_matches(doc: str, domain: str) -> bool:
    name = (doc or "").rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    tags = DOC_DOMAIN_TAGS.get(name)
    if tags is None:
        return True          # 未收录文档视为全域,不误杀
    return domain in tags


def rank_by_domain(rows: list, domain: str | None) -> list:
    """按检索域调整行序:off/无域=原样;boost=匹配前置(稳定);strict=过滤(空则回退)。"""
    mode = settings.recall_domain_mode
    if mode == "off" or not domain or not rows:
        return rows
    matching = [r for r in rows if _doc_matches(r.get("doc", ""), domain)]
    if mode == "strict":
        return matching if matching else rows
    others = [r for r in rows if not _doc_matches(r.get("doc", ""), domain)]
    return matching + others
```

- [ ] **Step 5: 接线三处**

5a. `kb.py`:`kb_recall(query: str | None, domain: str | None = None)`;`rows, backend = _fetch_rows(query)` 之后加:

```python
    from app.agent.recall.kb_tags import rank_by_domain
    rows = rank_by_domain(rows, domain)
```

5b. `service.py`:`build_recall_sections(memory_manager, query, include_kb=True, kb_domain=None)`;`kb = kb_recall(query)` 改为 `kb = kb_recall(query, domain=kb_domain)`。

5c. `chat.py` `_build_messages` 召回调用改为:

```python
            rr = build_recall_sections(self.memory_manager, recall_query,
                                       include_kb=include_kb,
                                       kb_domain=(qu.domain if qu is not None else None))
```

5d. `orchestrator.py` 在 `self._last_key = key` 之后加:

```python
        if qu is not None and qu.domain is None:
            qu.domain = key          # 粘性解析结果回填:检索过滤拿到确定域
```

- [ ] **Step 6: 跑测试确认通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_kb_domain_filter.py tests/test_recall_wiring.py tests/test_recall_kb.py tests/test_external_kb.py tests/test_recall_service.py tests/test_orchestrator_qu.py -q`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add app/agent/recall/kb_tags.py app/agent/recall/kb.py app/agent/recall/service.py app/agent/chat.py app/multi_agent/orchestrator.py app/config/settings.py tests/test_kb_domain_filter.py tests/test_recall_wiring.py
git commit -m "feat: 意图过滤检索——检索行按QU domain boost/strict排序过滤(文档2.2三级索引)"
```

---

### Task 7: 文档 + 全量回归 + 产品经理验收循环(协调者执行)

**Files:**
- Modify: `docs/企业级智能客服.md`(对照表:转人工闭环/来源引用/否定约束/FAQ缓存/意图过滤 五行置 ✅)
- Modify: `docs/统一召回层-作用与效果.md`(追加三件套章节,简述)
- Test: 后端回归子集 + vitest + 浏览器 PM 验收

- [ ] **Step 1: 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_escalation_loop.py tests/test_streaming_handoff.py tests/test_prompt_constraints.py tests/test_faq_cache.py tests/test_faq_serving.py tests/test_kb_domain_filter.py tests/test_recall_wiring.py tests/test_recall_kb.py tests/test_recall_service.py tests/test_external_kb.py tests/test_understanding.py tests/test_orchestrator_qu.py tests/test_orchestrator_unified.py tests/test_streaming_trace.py -q`
Expected: 全部通过
Run: `cd webui && npx vitest run && npx tsc --noEmit`
Expected: 全绿

- [ ] **Step 2: 文档更新并提交**(对照表五行 ❌候选/⚠️断链 → ✅,附一行实现说明)

- [ ] **Step 3: PM 验收循环(协调者亲自执行,不派实现子代理)**

1. 重启 ecom-agent 后端(加载新代码),确认 webui 运行
2. 派"产品经理"子代理(browser 工具),人设:电商客服产品经理,从真实用户视角在 http://localhost:5173 体验以下场景并逐项打分(满意/不满意+理由):
   - S1 发「转人工」→ 应零思考过程立即转接话术 + 🎧已转人工横幅
   - S2 连续三次发同一问题(如「退款怎么还没到」×3)→ 第三次应出现转人工横幅(原因含"重复")
   - S3 发「你们就是骗子,垃圾平台」→ 回复安抚 + 转人工横幅(负面情绪)
   - S4 发「下单后多久发货?」→ 思考面板出现「FAQ 秒答」,回复近即时
   - S5 发「价保多久」→ 回复带「(依据《…》)」真实来源标注
   - S6 检查全部回复无「我猜/可能是/应该是」
   - S7 发「退货运费谁承担」→ 预召回命中且来源合领域
   - 整体体验主观评价(语气/速度/可信度)
3. PM 报告不满意项 → 协调者定位修复(可派修复子代理)→ 重启后端 → 重新派 PM 验收
4. 循环上限 3 轮;3 轮仍有不满意项则如实向用户汇报剩余问题与建议
5. 每轮 PM 报告与修复记录写入 ledger
