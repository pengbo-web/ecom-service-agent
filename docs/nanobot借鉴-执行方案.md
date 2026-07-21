# nanobot 借鉴 · 优化执行方案（Master Plan）

> 本文是可直接照做的**执行方案**,把两份分析文档
> ([可改进点](借鉴nanobot-可改进点.md) / [详细分析与落地方案](nanobot借鉴-详细分析与落地方案.md))
> 的结论转成分阶段、带文件/接口/任务/验收/回滚的实施计划。
> 代码基线:分支 `feature/w1-service-streaming`(已含 W1–W4 + 议价 bargain + React 单页四合一 + 120+ 测试)。
> 每个阶段建议走本项目既有流程 `brainstorming → writing-plans → subagent-driven-development`(TDD + 逐任务双审 + 端到端冒烟)。

---

## 0. 当前基线核对(2026-07-21,合并后)

| 事实 | 结论 |
|------|------|
| `OpenAI(...)` 构造点 | 5 处:`app/agent/chat.py`、`app/multi_agent/orchestrator.py`、`app/agent/rag/embedder.py`、`app/evaluation/run_service.py`、`app/scripts/run_eval.py` |
| LLM 客户端复用关系 | `memory / summarizer / extraction / router` 均**复用传入的 client**——只要 chat/orchestrator 的 client 换成容错版,子调用自动继承 |
| 待建模块 | `app/resilience/`、`app/agent/consent.py`、`app/agent/history_utils.py` 均不存在 |
| chat.py 关键行 | `history_threshold`(:27)、`_react_loop`(:116)、`json.loads(tc.function.arguments)`(:154)、`_compress_history`(:247) |
| skills 解析 | `loader.py::_parse_frontmatter`(:35)手写正则,仅认扁平 `key: value` |
| 长期记忆 | `long_term.py::add_facts`(:85)= append + `lower()` 去重 + `[-max_facts:]` FIFO |
| 副作用工具 | `app/agent/tools/bargain.py`、`refund.py` 已存在,**执行前无消费门** |

---

## 1. 阶段总览与顺序

```
Phase 1  模型 fallback + 熔断          [P0·独立·最高可用性收益]  ← 先做
Phase 2  上下文防线三件套              [P0·独立·护 function-calling 循环]
Phase 3  风险动作前置授权门            [P1·贴合议价/退款涉钱场景]
Phase 4  鲁棒性阶梯(挑两条便宜的)     [P1·低成本减少可见报错]
Phase 5  记忆策展(借 prompt 不借基建) [P2·质量/成本优化]
Phase 6  治理链"观察 vs 授权"分离      [P2·架构优化,靠测试兜底]
Phase 7  小改进(Skill 解析 + 配置热更)[P3·顺手]
```

依赖关系:Phase 1–4 **互相独立**,可任意顺序/并行分支;Phase 3 与 Phase 6 有概念关联(授权门是 Phase 6 "授权"侧的具体落地),建议 3 先于 6。每个 Phase 结束都必须:**全量离线测试保持全绿 + 本阶段新增测试 + 服务启动冒烟**。

**不做**(见分析文档第五节):40+ provider 注册表、bwrap 沙箱、多渠道抽象、subagent 异步委派、FSM 状态机、mid-turn 注入、完整 Hook 类体系、SSRF(无外部调用面时)。

---

## Phase 1 — 模型 fallback + 熔断 + 错误分类重试  ⭐⭐⭐

**目标**:LLM 调用从"单点"变"主备双活":主模型瞬时错误(429/5xx/超时)自动重试,永久/可切换错误自动切备用模型,主模型连败触发熔断冷却。

**痛点**:5 处 `OpenAI()` 无重试/无 fallback/无熔断,主模型 429 即整条请求失败。

**新增/改动文件**
- 新增 `app/resilience/errors.py`:`classify_error(exc) -> "transient"|"switch"|"fatal"` + `retry_after_seconds(exc) -> float|None`。
- 新增 `app/resilience/breaker.py`:`CircuitBreaker(threshold=3, cooldown=60, now=time.time)`:`allow()/record_success()/record_failure()`。
- 新增 `app/resilience/llm_client.py`:`ResilientChatClient(primary, secondary, breaker, ...)` —— **透明代理**,暴露 `.chat.completions.create(**kw)` 与 `.beta.chat.completions.parse(**kw)`,其余属性 `__getattr__` 透传(与 W2 `TracingClient` 同款手法)。
- 新增 `app/resilience/factory.py`:`make_resilient_client() -> ResilientChatClient`(读 settings 建主备)。
- 改 `app/config/settings.py`:加 `fallback_base_url / fallback_model / fallback_api_key(可空,默认同主) / llm_timeout_s=120 / retry_backoff=(1,2,4) / breaker_threshold=3 / breaker_cooldown_s=60 / resilience_enabled=True`。
- 改 `app/agent/chat.py:18-21` 与 `app/multi_agent/orchestrator.py`:`self.client = make_resilient_client() if settings.resilience_enabled else OpenAI(...)`。**仅改构造点,不改 ReAct 逻辑**;memory/summarizer/extraction 因复用该 client 自动获容错。

**关键接口(供后续 writing-plans 定稿)**
```python
# errors.py
def classify_error(exc: Exception) -> str        # "transient" | "switch" | "fatal"
def retry_after_seconds(exc: Exception) -> float | None
# breaker.py
class CircuitBreaker:
    def allow(self) -> bool
    def record_success(self) -> None
    def record_failure(self) -> None
# llm_client.py
class ResilientChatClient:
    def __init__(self, primary, secondary, breaker, model, fallback_model,
                 backoff=(1,2,4), retry_after_cap=60): ...
    # .chat.completions.create(**kw) / .beta.chat.completions.parse(**kw) 透明代理
```

**任务拆解(TDD)**
1. `errors.classify_error` + `retry_after_seconds`:构造带 status_code / 文本的假异常,断言分类(429 拆速率限制=transient vs 欠费=switch;400/401/403/404/422=fatal;5xx/timeout=switch)。移植 `nanobot/providers/base.py:401-496,220-237,766-839`。
2. `CircuitBreaker`:注入假时钟,断言 3 连败跳闸、冷却内 `allow()==False`、冷却后半开放行。移植 `fallback_provider.py:108-115`。
3. `ResilientChatClient`:用 fake primary/secondary(可编程抛异常/返回),断言:瞬时错误按退避重试、遵守 Retry-After、可切换错误切 secondary、熔断打开直接走 secondary、fatal 直接抛。**全部离线,不连网络**。
4. `factory.make_resilient_client`:从 settings 建主备(secondary 缺省=复用主配置时退化为纯重试)。
5. 接入 chat.py / orchestrator.py 构造点;跑既有 `tests/test_emit.py`(离线构造 EcomAgent)确认无回归。
6. (可选)熔断命中数/fallback 命中率上报 W2:在 `create` 成功/切换时经现有 tracer 记一个 span 或计数,新增看板卡片。

**验收标准**
- [ ] 单测覆盖:分类 / 熔断 / 代理三层,全离线绿。
- [ ] 主模型注入 429 → 自动重试;注入欠费 → 切备用;备用成功返回。
- [ ] 主模型连败 3 次 → 熔断,后续直接走备用。
- [ ] `resilience_enabled=False` 时行为回退到原生 `OpenAI`(开关可关)。
- [ ] 全量离线测试仍全绿;`run_api.py` 起服务对话正常。

**风险与回滚**:透明代理若漏透传某属性会报 `AttributeError`——用 `__getattr__` 兜底(参照 TracingClient 的 `hasattr(real,'beta')` 防护)。开关 `resilience_enabled=False` 一键回退。
**工作量**:~1–1.5 人日。 **面试点**:"透明代理给 LLM 加错误分类重试 + 主备熔断,单点变双活。"

---

## Phase 2 — 上下文防线三件套  ⭐⭐⭐

**目标**:保护 function-calling 循环不被"单条大结果"撑爆、不被"悬空 tool_call"打成模型 400;压缩触发从"超 10 条"改为"按 token 预算"。

**痛点**(现状)
- `chat.py:91` 用 `len(raw_messages) > history_threshold(10)` 触发压缩:短对话过压、单条大结果失灵。
- `_compress_history:247` 只处理"起点是 tool 消息",没处理"assistant 带 tool_calls 但 tool 结果被裁掉"的非法态。
- tool 结果(整单 JSON / 长 RAG)无截断,直接进历史。

**新增/改动文件**
- 新增 `app/agent/history_utils.py`:
  - `estimate_tokens(messages) -> int`(粗估:字符数/一个系数,或用 tiktoken 若已依赖;先用字符估算)。
  - `legal_message_start(messages, start_idx) -> int`(把起点前移到合法 user 边界,不拆 tool_call/tool 对)。移植 `context_governance.py:437-446`。
  - `sanitize_tool_pairs(messages) -> list`(剥离畸形 tool_calls、丢弃悬空 tool 结果、回填缺失)。移植 `context_governance.py:176-296`。
  - `truncate_tool_result(text, limit) -> str`(超长截断 + 提示"结果过长已截断")。移植 `context_governance.py:109-136` 的思路(先不落盘,纯截断)。
- 改 `app/agent/tools/registry.py::execute_tool`(或 chat.py 追加 tool 消息处):对 `result_str` 调 `truncate_tool_result`。
- 改 `app/agent/chat.py`:`_compress_history` 用 token 预算触发 + `legal_message_start`;`_build_messages` 送模型前调 `sanitize_tool_pairs`。
- 改 `app/config/settings.py`:加 `context_window_tokens / max_output_tokens / context_safety_buffer=1024 / tool_result_max_chars=4000`。

**任务拆解(TDD)**
1. `truncate_tool_result`:超长截断、保留头部、加提示;短结果原样。→ 接入 execute_tool,测 tool 结果被截断。
2. `sanitize_tool_pairs`:构造"assistant 有 tool_calls 但缺对应 tool 消息""悬空 tool 消息"等非法序列,断言修复后合法(每个 tool_calls 都有配对 tool,反之亦然)。
3. `legal_message_start`:构造在 tool 中间的起点,断言前移到 user 边界。
4. `estimate_tokens` + 预算触发:把 `_compress_history` 触发条件改为 `estimate_tokens(build_messages()) > window - out - buffer`;测短对话不压、超预算才压。
5. `_build_messages` 末尾调 `sanitize_tool_pairs`;跑既有 conversation/react 相关的离线测试。

**验收标准**
- [ ] 单条超大 tool 结果被截断,历史不再爆窗。
- [ ] 构造"悬空 tool_call"序列 → 送模型前被自愈成合法序列(单测)。
- [ ] 压缩由 token 预算触发,短对话不再被过度压缩。
- [ ] 全量离线测试全绿。

**风险与回滚**:token 估算不准可能过早/过晚压缩——先用保守系数,后续可换 tiktoken。改动集中在 history_utils(纯函数,易测)+ chat.py 两处调用。
**工作量**:~1 人日。 **面试点**:"上下文防线:大结果截断 + token 预算压缩 + 裁剪协议自愈,防长对话把模型打成 400。"

---

## Phase 3 — 风险动作「前置授权门」  ⭐⭐⭐

**目标**:退款 / 大额让价 / 订单状态变更等**副作用工具在执行前**必须有授权(用户确认或坐席放行),默认拒绝;从"先斩后奏 + 事后转人工"变"事前门控"。

**痛点**:HITL 是回复后才判断转人工(`streaming.py`);`bargain.negotiate_price`、`refund.apply_refund` 由模型自行决定就执行,涉钱无前置确认。

**新增/改动文件**
- 新增 `app/agent/consent.py`(仿 `nanobot/agent/goal_permission.py`,~30 行):
  ```python
  _ALLOWED: ContextVar[frozenset[str]] = ContextVar("consent_allowed", default=frozenset())
  def is_allowed(action: str) -> bool
  @contextmanager
  def consent_scope(actions: frozenset[str]): ...   # 进入授权、退出复位
  ```
- 改 `app/agent/tools/refund.py` / `bargain.py`:执行前 `if not is_allowed("refund"/"deep_discount"): return {"success": False, "need_confirm": True, "message": "该操作需您确认…"}`。
- 改 `app/api/streaming.py` / `chat.py`:把"本轮已获授权的动作集合"通过 `consent_scope` 压入(来源:① 用户上一轮明确确认;② 坐席在 HITL 面板放行)。授权在动作边界强制、用后即撤。
- 可选:议价"深让价"阈值——`negotiate_price` 让价超过某比例才需授权(小让价免确认)。

**任务拆解(TDD)**
1. `consent.py`:测默认拒绝、`consent_scope` 内放行、退出复位、嵌套安全。
2. `refund.apply_refund`:未授权返回 `need_confirm`;授权(在 `consent_scope`)内正常执行改库。
3. `bargain.negotiate_price`:让价超阈值未授权 → `need_confirm`;小让价 → 直接成交。
4. 服务层:模拟"用户确认→本轮授权→再次调用成功"的两轮流程(用 fake agent)。

**验收标准**
- [ ] 未授权时退款/深让价返回"需确认",**不改库/不成交**。
- [ ] 授权后同一动作正常执行。
- [ ] 授权粒度到"动作类型",每轮生效、用后复位(单测验证不泄漏到下一轮)。

**风险与回滚**:授权来源(用户确认的判定)要清晰,避免误判导致正常流程被卡;可先只对 refund 上门控,议价随后。
**工作量**:~1 人日。 **面试点**:"仿 ContextVar 授权门做默认拒绝的前置确认,涉钱动作事前门控而非事后补救。"

---

## Phase 4 — 鲁棒性阶梯(挑两条便宜的)  ⭐⭐

**目标**:减少用户可见的"⚠️ 出错了"。

**改动文件**:`app/agent/chat.py::_react_loop`(:116)。
**两条**:
1. **空回复重试**:LLM 返回空 `content` 且无 tool_calls → 重试一次(移植 `runner.py:494` 思路)。
2. **畸形工具调用降级**:`json.loads(tc.function.arguments)` 失败或工具名缺失 → 退回一次"不带 tools"的请求让模型用自然语言回答(移植 `runner.py:821-848`)。

**任务拆解(TDD)**:用 fake client 让首个响应空/畸形、第二个正常,断言最终有合理回复而非异常。
**验收**:[ ] 空回复/畸形工具调用不再直接冒泡成用户可见错误;全量离线绿。
**工作量**:~0.5 人日。

---

## Phase 5 — 记忆策展(借 prompt,不借基建)  ⭐⭐

**目标**:长期记忆从"追加+去重+FIFO 截 50"升级为"LLM 策展"(合并/就地纠正/按龄衰减/删或留),减少近义重复与重要事实被挤掉;可选离线化。

**痛点**:`long_term.py:85 add_facts` 精确小写去重、`[-50:]` FIFO——近义重复留存、老而重要被新琐事挤掉,且占在线成本。

**改动文件**
- 改 `app/agent/memory/extraction.py` / `long_term.py`:会话结束的记忆更新改为一次 **LLM 策展调用**(prompt 借 nanobot `consolidator_archive.md` 的 SNIP 标记 `[permanent]/[durable]/[ephemeral]/[correction]/[skip]` 与 MECE 去重规则)。
- 可选:LTM 按 MECE 拆"用户偏好/行为规则"两段注入。
- 进阶(可选):新增 `app/scripts/reflect_memory.py` 离线 cron,整体回看会话→归纳→只在离线写画像;在线只读。

**跳过**:git 版本化、dream-log、append-only history 运行时、Dream 文件编辑引擎(我们"每用户封顶 50 条"规模下过度工程)。

**任务拆解(TDD)**:策展函数用 fake client 返回结构化策展结果,断言合并/纠正/衰减逻辑;prompt 内容单独评审。
**验收**:[ ] 近义事实被合并、纠正生效、按龄衰减;记忆总量受控且更干净。
**工作量**:~1–1.5 人日(prompt 调试占大头)。

> 通用原则复用:判断离线批处理是否真生效,看**真实产出**不信模型自称——与 `reflow`/`run_eval` 一脉相承。

---

## Phase 6 — 治理链"观察 vs 授权"分离  ⭐⭐(架构优化)

**目标**:把 `streaming.py` 里命令式揉在一起的治理链(限流→人工→快路径→成本→护栏→Agent→护栏→升级)理清为两类:**观察**(不能否决动作:可观测埋点、事后升级判定)与**授权**(在动作边界强制:限流/成本/人工短路/前置授权门)。

**关键判断(如实记录)**:对单循环小 bot,照搬 nanobot 完整 `AgentHook`/`CompositeHook`/工厂体系属**过度工程**。**只做轻量版**:
- 输出护栏做成一个 `finalize(text)->text` 变换回调(借 `hook.py:139 finalize_content` 思路)。
- 2–3 个工具生命周期回调(`before/after_execute_tool`)替换 `_react_loop` 内联 `_emit`,得到干净埋点缝。
- **暂不**引入完整 hook 类体系。

**改动文件**:`app/api/streaming.py`(治理链重排)、`app/agent/chat.py`(埋点缝)。
**前置**:建议在 Phase 1–4 稳定后做;有 120+ 测试兜底,重构风险可控。
**验收**:[ ] 行为等价(全量离线测试全绿 + 端到端冒烟);治理项之间解耦、可独立开关。
**工作量**:~1.5–2 人日。 **面试点**:"把'观察'与'授权'分离——观察不能否决动作,授权在动作边界强制。"

---

## Phase 7 — 小改进  ⭐

**7a. Skill frontmatter 解析健壮性**
- 改 `app/agent/skills/loader.py::_parse_frontmatter(:35)`:手写正则 → `yaml.safe_load`(移植 `skills.py:250`)。当前只认扁平 `key: value`,遇嵌套/多行会崩。
- 需在 `requirements.txt` 加 `pyyaml`(或确认已随 fastapi 传入)。
- 可选:frontmatter 加 `requires.env`,启动检测缺失内部 key,不可用技能标灰 + 原因(移植 `skills.py:144-214`)。

**7b. 配置签名式热更新**
- 给 `app/config/` 加"配置签名"函数 + 惰性比对:每次取 runtime 时比对影响行为字段(`hitl_confidence_threshold`/`rate_limit_per_min`/主备模型),变了才重建对象——线上调阈值/切模型免重启(移植 `config/loader.py` 思路,不 watch 文件)。

**工作量**:各 ~0.5 人日。

---

## 3. 全局验收与回归(每个 Phase 通用)

1. **离线测试基线**:每个 Phase 完成后,`.venv/Scripts/python.exe -m pytest tests/ -q`(排除依赖真实 API 的 test_agent/test_rag/test_mcp/test_react_agent/test_multi_agent/test_memory/test_skills/test_evaluation/test_conversation_management)必须**全绿**,且本阶段新增测试通过。
2. **端到端冒烟**:`run_api.py` 起服务,聊 2–3 句(含一个会触发本阶段能力的场景),看行为正确。
3. **开关兜底**:每个新能力带 settings 开关,默认开、可关(便于对比与回滚)。
4. **提交规范**:每阶段独立特性分支或独立提交序列,commit 信息带阶段号;文档链接回本方案。

---

## 4. 推荐执行顺序(结合简历/面试价值)

| 顺序 | Phase | 理由 |
|------|-------|------|
| 1 | Phase 1 模型 fallback | 最独立、可用性收益最大、面试最好讲、复用 TracingClient 手法 |
| 2 | Phase 2 上下文防线 | 护 function-calling 循环,长对话稳定性 |
| 3 | Phase 3 前置授权门 | 贴合已有议价/退款涉钱场景,安全叙事强 |
| 4 | Phase 4 鲁棒性阶梯 | 便宜、直接改善体验 |
| 5 | Phase 5 记忆策展 | 质量/成本优化,想深化记忆线时做 |
| 6 | Phase 6 治理链分离 | 架构升华,放稳定后做 |
| 7 | Phase 7 小改进 | 顺手补 |

> 建议先做 **Phase 1 + 2 + 3**(约 3–4 人日),这三块把"可用性 / 长对话稳定性 / 涉钱安全"补齐,是最能体现"生产级"的组合,也最好写进简历/面试。

---

## 5. 每 Phase 的实现入口(照此开工)

对选定的 Phase:
1. `Skill(brainstorming)` 确认该 Phase 的范围与取舍(尤其 Phase 5/6 有设计空间)。
2. `Skill(writing-plans)` 基于本方案该 Phase 的"文件/接口/任务拆解"产出 bite-sized TDD 实现计划。
3. `Skill(subagent-driven-development)` 或本会话内直接执行:先写失败测试 → 最小实现 → 跑过 → 提交。
4. 完成后按第 3 节做全局验收 + 端到端冒烟,推送。

---

> 依据:两份分析文档 + 对当前分支代码的核对(见第 0 节)。nanobot 源码位置见《详细分析与落地方案》附录索引。本项目与 nanobot 均 MIT,借鉴做法/思路即可,保留必要出处。
