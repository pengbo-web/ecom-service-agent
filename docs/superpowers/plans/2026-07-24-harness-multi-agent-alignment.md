# Harness 多 Agent 架构对齐 实现方案(H1–H4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 把项目**严格对齐**行业电商客服 **Harness 架构**——领域路由改为 **售前/售中/售后**;补齐功能分工(出话/评估/润色 + **选择器动态调度 G1**)、FTS5 记忆管理 + **结构化记忆档案 G2**、Skill 自动化生成 **完整闭环 G3**、PE 自动化数据飞轮 + **标注数据采集 G4**(RL 除外),全部**手写**、保留**通用电商**业务。

**Architecture:** **总控 Agent(多 Agent 编排)= 唯一运行架构**,移除"单 Agent 独立模式";`EcomAgent` 降为被总控驱动的**内部 ReAct 引擎**(不删,B1 已统一复用)。两层 Agent = 第一层领域路由(**售前/售中/售后** 三域)+ 第二层功能分工(总控内 出话→评估→润色,由 **LLM ReAct 选择器**动态调度、可循环)。外围 Harness 能力层:双层记忆(短期已有 + 长期扩为**结构化档案**:base profile/行为标签/工单流转)+ FTS5 记忆管理(新)+ 上下文引擎(已有)+ Skills 管理(已有)+ **Skill 自动生成闭环**(创建/自改进/用户建模,新)+ Tools/MCP(已有)+ HITL(已有)+ 数据飞轮(PE 自动化 + **标注数据采集**,新)。

**Tech Stack:** Python 3.11、现有 EcomAgent ReAct 引擎、SQLite FTS5(内置)、Redis(会话存储,已有)、OpenAI 兼容 LLM;测试用 fake client / fakeredis / 临时目录,不触网。

## Global Constraints

- **手写实现,不引入 AutoGen / LangGraph 等重依赖**;对齐架构形态而非厂商。
- **总控(多 Agent)为唯一运行架构**:移除单 Agent 独立模式——API/CLI 入口恒走总控;`EcomAgent` 保留为总控驱动的内部引擎(不删);`multi_agent_enabled` 废弃(恒 True)。单元测试仍可直接构造 `EcomAgent` 测引擎。
- **领域路由 = 售前 / 售中 / 售后 三域**(替代原 售前/售后/投诉;投诉并入售后)。
- **保留通用电商业务**,不新建得物式尺码/spuid 工具与数据。
- **严格对齐补齐 G1–G4**:选择器动态调度、结构化记忆档案、Skill 闭环、标注采集。高风险/重成本项(如 Skill 自改进、LLM 选择器)做**半自动/可关**,但形态必须在。
- **分级门控**:功能层(选择器/评估/润色)仅"复杂轮"(本轮调用过工具)全走,简单轮直接用草稿,省 LLM 成本(尤其 LLM 选择器每步一次调用,更需门控)。
- **每个新能力带 `settings` 开关,默认开、可关**;关闭即回退到改造前行为。
- **接地铁律**:评估/重写必须带本轮工具真实结果,重写不脱离事实;评估器异常 **fail-open**(不阻断回复)。
- **向后兼容**:记忆/存储改动兼容旧 JSON / 旧库;老会话可读。
- 每阶段:**独立提交 + 离线全量绿 + 端到端冒烟(真 Redis / fakeredis)+ commit 带阶段号**。
- **不做 Agentic RL 训练**(数据飞轮只做 PE 自动化侧)。

## File Structure

| 文件 | 阶段 | 责任 |
|---|---|---|
| `app/multi_agent/orchestrator.py`(改) | H1.0 | **显式化"总控 Agent"**:内聚暴露 react/memory/permissions/lifecycle 四项职责 |
| `app/api/session_manager.py` / `main.py` / `run_eval.py`(改) | H1.0-C | **移除单 Agent 模式**:工厂恒建总控;`multi_agent_enabled` 废弃 |
| `app/multi_agent/router.py` / `agents.py` / `app/prompts/agents.py`(改) | H1.0 | 领域改 售前/售中/售后 + 三域画像/工具子集 |
| `app/prompts/reply_pipeline.py`(新) | H1 | 评估器 / 重写 / 润色 / **选择器(G1)** 提示词 |
| `app/agent/reply_pipeline.py`(新) | H1 | 出话/评估/(重写)/润色 + **LLM ReAct 选择器循环(G1,规则兜底)** |
| `app/agent/chat.py`(改) | H1 | ReAct 产出草稿后接入流水线 + 接地上下文提取 |
| `app/config/settings.py`(改) | H1-H4 | 各能力开关 |
| `app/agent/memory/fts_store.py`(新) | H2 | SQLite FTS5 记忆全文索引:写入 / 关键词召回 |
| `app/agent/memory/profile.py`(新) | H2/G2 | **结构化用户档案**:base profile / 行为标签 / 工单流转 |
| `app/agent/memory/long_term.py`(改) | H2 | 事实/档案 写入+召回接 FTS5(与 LLM 策展并存) |
| `app/agent/skills/synthesizer.py`(新) | H3 | 归档聚类 → 生成候选 skill;**失败自改进(G3)** |
| `app/agent/skills/user_modeling.py`(新) | H3/G3 | **从行为建模用户偏好** → 写档案/标签 |
| `app/scripts/synthesize_skills.py`(新) | H3 | 离线闭环入口(创建/自改进/用户建模,半自动) |
| `app/labeling/store.py`(新) | H4/G4 | **标注数据采集**:意图/话术/正确性/润色 标注表 + 半自动打标 |
| `app/evaluation/pe_optimizer.py`(新) | H4 | 从标注数据产出提示词改进候选(供人工采纳) |
| `tests/test_reply_pipeline.py` / `test_memory_fts.py` / `test_memory_profile.py` / `test_skill_synth.py` / `test_user_modeling.py` / `test_labeling.py` / `test_pe_optimizer.py`(新) | H1-H4 | 各阶段离线测试(fake client) |

---

## Phase H1 — 功能型多 Agent(出话 / 评估器 / 润色)

**Interfaces:**
- Consumes:现有 `EcomAgent._react_loop()` 的 `final_text`(= 出话草稿)、`self._step_seq`(>0 表本轮用过工具 = 复杂轮)、`self.raw_messages`(取本轮 tool 结果做接地)。
- Produces:`ReplyPipeline.run(client, model, user_input, draft, grounding, complex_turn, emit) -> str`(最终回复);发 `evaluate`/`polish` 事件供 trace。

### Task H1.0 — 显式化总控 Agent + 领域路由改 售前/售中/售后

**A) 领域路由改 售前/售中/售后**
- [ ] **写测试**:`tests/test_orchestrator_unified.py` 更新——`profiles` 键为 `{"presale","midsale","aftersale"}`;售中含 `query_logistics/expedite_shipping/change_address/cancel_order`;售后含 `apply_refund/issue_invoice`;售前含 `query_product/query_coupons/negotiate_price`。
- [ ] **实现**:`router.py` `VALID_AGENTS={"presale","midsale","aftersale"}`、`DEFAULT_AGENT="aftersale"`;`prompts/agents.py` 用 `PRESALE/MIDSALE/AFTERSALE_PROMPT`(投诉话术并入售后);`agents.py` 三域画像 + 工具子集(见上)。

**B) 显式化"总控 Agent"(把散落的四项能力内聚成一处入口)**
> 目标:架构图里那个蓝盒子在代码里有对应的**一个类/一处入口**,四项能力(React 机制、记忆管理、业务权限、生命周期)显式可见、可指认——不再隐式散落。`MultiAgentOrchestrator` 即"总控 Agent"。
- [ ] **写测试**:`tests/test_controller_agent.py`——`MultiAgentOrchestrator` 暴露四个只读职责入口:
  - `react`:返回其 ReAct 引擎(即 `self.engine`,拥有 `_react_loop`);
  - `memory`:返回 `memory_manager`;
  - `permissions`:返回受控风险动作集(= `RISK_ACTIONS`)+ 是否已接幂等/挂起(标识业务权限层);
  - `lifecycle`:暴露 `save/close/reset/history_size` + 当前 `status/step_seq`(本会话生命周期;跨会话回收在 SessionManager,文档标注)。
  - 断言 `capabilities()` 返回这四项的清单(供自省/文档)。
- [ ] **实现**:在 `orchestrator.py` 给 `MultiAgentOrchestrator` 加类 docstring 明确"总控 Agent"定位 + 上述四个 `@property`/方法(多为对已有能力的**内聚暴露**,不改行为):`react`→`self.engine`;`memory`→`self.engine.memory_manager`;`permissions`→`{"risk_actions": RISK_ACTIONS, "consent": True, "idempotency": True, "escalation(HITL)": True}`;`lifecycle`→委托 save/close/reset(已有)+ `status`/`step_seq`(读 engine);`capabilities()` 汇总四项名称。
- [ ] 运行相关测试 + 全量离线绿。提交:`refactor(multi-agent): H1.0 显式化总控Agent + 领域改售前/售中/售后`。

**C) 移除单 Agent 运行模式(总控为唯一入口)**
> 只保留多 Agent(总控)架构;`EcomAgent` 不删,继续作为被总控驱动的内部 ReAct 引擎。
- [ ] **写测试**:`tests/test_session_manager.py`(或新增)——`SessionManager._default_factory` 恒返回 `MultiAgentOrchestrator`(不再依赖 `multi_agent_enabled`);API 走 `/api/chat` 时 `manager.get_or_create` 得到总控实例。
- [ ] **实现**:
  - `session_manager.py::_default_factory`:去掉 `if settings.multi_agent_enabled` 分支,**恒建 `MultiAgentOrchestrator`**。
  - `settings.multi_agent_enabled`:标注**废弃**(保留字段避免破坏 .env,值恒当 True 处理)或删除并清理引用(`app.py` 的 eval `_mode`、`run_eval.py` 默认改为 multi)。
  - `EcomAgent` 类 docstring 更新:"内部 ReAct 引擎,由总控 Agent(MultiAgentOrchestrator)驱动;不再作为独立运行模式"。
  - `main.py`(CLI):改为构造 `MultiAgentOrchestrator`(与 API 一致),或标注 CLI 仅供引擎级调试。
- [ ] 运行全量离线绿(注意:直接构造 `EcomAgent` 的单元测试仍有效,测的是引擎;走工厂/streaming 的测试现在拿到总控)。
- [ ] **端到端冒烟**:真 Redis 起服务,默认(无需设 `MULTI_AGENT_ENABLED`)聊一句 → 事件流出现 `route`(总控路由),确认走的是总控。
- [ ] 提交:`refactor(multi-agent): H1.0 移除单Agent模式,总控为唯一运行架构`。

### Task H1.1 — 提示词(评估/重写/润色)
- [ ] **写测试**:`tests/test_reply_pipeline.py` 断言三提示词非空且含关键约束词("接地"/"不得改变任何事实")。
- [ ] **实现** `app/prompts/reply_pipeline.py`:`EVALUATOR_PROMPT`(四维度:接地/准确/合规/完整,输出 JSON `{ok,issues,suggestion}`)、`REDRAFT_PROMPT`(据工具真实结果重写、不编造)、`POLISH_PROMPT`(小夕人设,铁律:金额/日期/状态/结论原样保留)。
- [ ] 运行:`.venv/Scripts/python.exe -m pytest tests/test_reply_pipeline.py -q` → 提示词测试通过。
- [ ] 提交。

### Task H1.2 — ReplyPipeline + LLM ReAct 选择器动态调度(G1,默认 LLM,规则兜底)
> 100% 对齐图中"总控 ReAct 编排出话/评估/润色":**总控每步真用 LLM 推理决定下一个功能 Agent**(next ∈ evaluate/redraft/polish/done),可循环;`selector_mode` 默认 `llm`,LLM 失败/关闭时退回**规则选择器**保稳。
- [ ] **写测试**(fake client 脚本化):
  - 简单轮(`complex_turn=False`)→ 原样返回草稿(门控)。
  - **LLM 选择器**:脚本让选择器依次吐 `evaluate`→(评估 ok)→`polish`→`done` → 得润色文本;验证每步"选择"来自 LLM 输出。
  - **循环**:选择器吐 `evaluate`→(评估 not ok)→`redraft`→`evaluate`→`polish`→`done`,验证重写后**再评估**。
  - 达 `max_rounds` → 强制收敛到 polish→done(选择器再想 redraft 也不再执行)。
  - `selector_mode="rule"` 或 LLM 选择器输出非法 → 退回规则选择器,流程仍走通。
  - `reply_pipeline_enabled=False` → 原样返回草稿;评估坏 JSON → fail-open。
- [ ] **实现** `app/prompts/reply_pipeline.py` 增 `SELECTOR_PROMPT`(输入:用户问题/当前草稿/最近评估结论/已进行轮次;输出 JSON `{"next": "evaluate|redraft|polish|done", "reason": "..."}`)。
- [ ] **实现** `app/agent/reply_pipeline.py`:
  - `FunctionalSelector`:`choose_llm(client, model, state) -> role`(LLM 推理选下一步,解析失败抛出)+ `choose_rule(state) -> role`(规则兜底:未评估→evaluate;评估不 ok 且未达 max→redraft;否则 polish;润色后 done)。
  - `ReplyPipeline.run(...)`:`state={draft, verdict, polished, rounds}`;循环 `role = selector(mode)`,`selector_mode=="llm"` 先试 `choose_llm`、异常/非法则 `choose_rule`;按 role 分派 `_evaluate/_redraft/_polish`;达 `max_rounds` 后选择器只允许 polish/done(防 LLM 无限重写);每步发 `select`(带 next+reason)/`evaluate`/`polish` 事件供 trace。
  - `settings.reply_pipeline_max_rounds`(默认 2)、`settings.selector_mode`(默认 `"llm"`)。异常 fail-open/回退上一版。
- [ ] 运行该测试文件 + 全量离线绿。提交:`feat(agent): H1 出话/评估/润色 + LLM ReAct 选择器动态调度(G1)`。

### Task H1.3 — 接入 EcomAgent + 分级门控 + 接地上下文
- [ ] **写测试**:用现有裸 agent 模式(`test_react_degrade` 风格)驱动 `chat()`,断言:复杂轮(mock `_step_seq>0`)会调用 pipeline;简单轮不调用;`_grounding_context()` 只取"最后一条 user 之后的 tool 结果"。
- [ ] **实现**:
  - `EcomAgent.__init__`:`self._reply_pipeline = ReplyPipeline()`。
  - `EcomAgent._grounding_context()`:逆序取 raw_messages 到最近一条 user 为止的 tool 消息内容(每条截断 500 字)。
  - `chat()`:`final_text = self._react_loop()` 后,`final_text = self._reply_pipeline.run(self.client, self.model, user_input, final_text, self._grounding_context(), self._step_seq>0, self._emit)`,再 `_extract_structured_response`。
  - `settings.reply_pipeline_enabled = True`。
- [ ] 运行全量离线(排除 API 依赖集)绿。
- [ ] **端到端冒烟**:真 Redis 起服务,问"查订单 ORD-20240115-001"(复杂轮)→ 事件流出现 `evaluate`+`polish`;问命中 fast-path 的"你好"→ 不触发。
- [ ] 提交:`feat(agent): H1 功能型多Agent 出话/评估/润色(手写+分级门控)`。

**验收**:复杂轮回复经评估+润色、接地不谎报、更拟人;简单轮零额外成本;关开关即回退。**工作量**:~2.5 人日。

---

## Phase H2 — FTS5 记忆管理(SQLite 全文索引召回)

**Interfaces:**
- Produces:`MemoryFtsStore.index(user_id, fact_id, content)`、`MemoryFtsStore.search(user_id, query, top_k) -> list[str]`。
- Consumes:`LongTermMemory` 的 facts(写入时同步索引;召回时 FTS 关键词 + 现有全量并集)。

### Task H2.1 — FTS5 存储层
- [ ] **前置检查**:`.venv/Scripts/python.exe -c "import sqlite3;print('FTS5' in sqlite3.connect(':memory:').execute('pragma compile_options').fetchall().__str__() or True)"`;实测建 `CREATE VIRTUAL TABLE ... USING fts5(...)` 是否可用(现代 Python 内置)。若不可用,降级为 LIKE 关键词匹配(方案 B,同接口)。
- [ ] **写测试**:`tests/test_memory_fts.py`(临时 db):index 三条事实 → search 关键词命中相关条、不命中无关条;按 user_id 隔离;top_k 生效。
- [ ] **实现** `app/agent/memory/fts_store.py`:虚拟表 `mem_fts(user_id, fact_id, content)` USING fts5;`index/search/clear`;WAL 模式(`PRAGMA journal_mode=WAL`)。
- [ ] 运行绿。提交。

### Task H2.2 — 接入 LongTermMemory(混合召回)
- [ ] **写测试**:facts 写入时被索引;`recall(query)` 返回"FTS 命中的相关事实"优先(与现有注入并存,不破坏 build_prompt_section)。
- [ ] **实现**:`LongTermMemory.add_facts/curate` 后同步 `fts.index`;新增 `recall(query, top_k)` 用 FTS 召回;`build_memory_prompt_sections` 可选按当前 query 召回 top-k(而非全量注入),受 `settings.memory_fts_enabled` 门控,默认开;关闭回退全量注入。
- [ ] 运行全量离线绿(注意 conftest 隔离临时 db)。
- [ ] **冒烟**:多轮对话后,相关 query 能召回对应历史事实。
- [ ] 提交:`feat(memory): H2 FTS5 记忆全文索引召回(混合)`。

### Task H2.3 — 结构化用户档案(G2:base profile / 行为标签 / 工单流转)
- [ ] **写测试**:`tests/test_memory_profile.py`——`UserProfile` 有 `base`(会员等级/联系方式等)/`tags`(行为标签)/`tickets`(工单流转记录);`update_base/add_tag/add_ticket` 落库(SQLite,按 user_id);`to_prompt()` 生成注入片段;向后兼容(无档案返回空片段)。
- [ ] **实现** `app/agent/memory/profile.py`:`UserProfile` + SQLite 表 `user_profile(user_id, base_json, tags_json, updated_at)` 与 `user_tickets(user_id, ticket_id, status, reason, ts)`(工单流转);读写方法。
  - **base profile 来源**:会员等级/联系方式从 `users`/account_ops 同步;**行为标签**来源见 H3 用户建模(先留写接口);**工单流转**:HITL 升级时写一条 ticket(接 `hitl.escalate`)。
- [ ] **接入**:`LongTermMemory.build_memory_prompt_sections` 增注入 `UserProfile.to_prompt()`;`hitl.escalate` 落工单记录;受 `settings.memory_profile_enabled`(默认开)门控。
- [ ] 运行全量离线绿。提交:`feat(memory): H2/G2 结构化用户档案(profile/行为标签/工单流转)`。

**验收**:记忆按 query 关键词精准召回、按 user 隔离;长期记忆含结构化档案(会员/标签/工单)并注入 prompt;转人工时工单流转有记录。**工作量**:~2.5 人日(含 G2)。

---

## Phase H3 — Skill 自动化生成 v1(半自动)

> 半自动:离线从"归档的成功会话"聚类重复模式 → LLM 生成候选 skill markdown → 落到 `definitions/_candidates/` 供**人工审核**入库。**不做**全自动自改进闭环(高风险,后续再说)。

**Interfaces:**
- Consumes:`session_archive` 表(R5 已落)的会话样本。
- Produces:`synthesize_skills(client, model, samples, out_dir) -> list[Path]`(候选 skill 文件路径)。

### Task H3.1 — 会话聚类 + skill 合成
- [ ] **写测试**:`tests/test_skill_synth.py`(fake client):给若干"同类已解决会话"样本 → 合成器产出 1 个候选 skill(name/description/steps 齐全的 markdown);坏 LLM 输出 → 跳过不崩;空样本 → 产 0 个。
- [ ] **实现** `app/agent/skills/synthesizer.py`:
  - `group_samples(archived) -> dict[intent, list]`(按 top 意图/关键词粗聚类)。
  - `synthesize_one(client, model, group) -> dict|None`(LLM 输出 skill frontmatter+body;复用 `_parse_frontmatter` 校验)。
  - `synthesize_skills(...)`:遍历聚类产候选,写入 `out_dir`(默认 `definitions/_candidates/`)。
- [ ] 提交。

### Task H3.2 — 离线入口 + 审核流
- [ ] **实现** `app/scripts/synthesize_skills.py`:读 `session_archive` 近 N 条 → `synthesize_skills` → 打印候选路径 + 提示"人工审核后移入 definitions/ 生效"。
- [ ] `settings.skill_synth_enabled`(默认 False:仅离线手动跑)。
- [ ] **冒烟**:造几条同类归档 → 跑脚本 → `_candidates/` 出现候选 skill markdown,`_parse_frontmatter` 能解析。
- [ ] 提交:`feat(skills): H3 Skill 自动化生成 v1(半自动,离线合成候选)`。

### Task H3.3 — Skill 失败自改进(G3)
- [ ] **写测试**:`tests/test_skill_synth.py` 增——给"某 skill + 该 skill 相关的失败会话(转人工/低分)"→ `improve_skill(client, model, skill, failure_cases)` 产出改进版 skill 候选;无失败样本→不改。
- [ ] **实现** `synthesizer.py::improve_skill`:LLM 输入 现 skill body + 失败案例 → 输出改进版(写 `_candidates/`,人工审核替换)。失败案例来源:trace 低分 / HITL 升级且当轮 load 过该 skill。
- [ ] 提交。

### Task H3.4 — 用户建模(G3:从行为推断偏好)
- [ ] **写测试**:`tests/test_user_modeling.py`(fake client)——给某用户归档会话样本 → `model_user(client, model, user_id, samples)` 产出行为标签列表(如"偏好红色/常问物流/价格敏感")→ 写入 H2 的 `UserProfile.tags`;空样本→空。
- [ ] **实现** `app/agent/skills/user_modeling.py::model_user`:LLM 从行为归纳偏好标签 → `UserProfile.add_tag`。
- [ ] 提交。

### Task H3.5 — 自述化闭环入口
- [ ] **实现** `app/scripts/synthesize_skills.py` 扩为完整离线闭环:读归档 → ①用户建模(写档案标签)②聚类创建候选 skill ③对已入库 skill 跑失败自改进 → 全部产候选/更新供人工确认;打印摘要。
- [ ] `settings.skill_synth_enabled`(默认 False,离线手动)。
- [ ] **冒烟**:造归档样本 → 跑脚本 → 产出 候选 skill + 用户标签更新 + skill 改进候选。
- [ ] 提交:`feat(skills): H3/G3 Skill 自动化闭环(创建/自改进/用户建模,半自动)`。

**验收**:离线闭环能从归档产出——①合法候选 skill ②用户行为标签(写入档案)③失败 skill 的改进候选;均半自动(产候选,人工确认)。**工作量**:~4 人日(风险最高,故全程半自动)。

---

## Phase H4 — PE 自动化(数据飞轮)

> 从标注数据(回复正确性标注 / 优秀客服润色数据 / 转人工原因)离线产出**提示词改进候选**,供人工采纳。轻量、与现有 eval 回归门禁衔接;**不自动改线上 prompt**。

**Interfaces:**
- Consumes:**H4.0 的 `reply_labels`**(正确性/润色标注)+ eval 失败用例 / trace 低分会话。
- Produces:`propose_prompt_tweaks(client, model, bad_cases) -> list[str]`(改进建议)。

### Task H4.0 — 标注数据采集(G4:半自动)
> 图里数据飞轮吃 意图/话术/正确性/润色 标注。做**最小半自动采集**:评估器 verdict 自动打"正确性标注",HITL 坐席纠正为"金标",润色前后成对存"优秀润色数据"。
- [ ] **写测试**:`tests/test_labeling.py`(临时 db)——`label_reply(session_id, intent, draft, final, verdict, corrected)` 落库 `reply_labels`;`list_labels(kind)` 取"正确性/润色/意图"三类;半自动来源:评估器 ok→auto 正确标、坐席纠正→gold。
- [ ] **实现** `app/labeling/store.py`:表 `reply_labels(id, session_id, intent, draft, final, correct, source[auto/hitl], ts)`;`label_reply/list_labels`;在 `ReplyPipeline`(评估后自动打标)与 HITL(坐席纠正打 gold)处埋点采集;`settings.labeling_enabled`(默认开,best-effort)。
- [ ] 运行绿。提交:`feat(labeling): H4/G4 标注数据采集(评估器/HITL 半自动打标)`。

### Task H4.1 — 失败用例归纳 + 改进建议
- [ ] **写测试**:`tests/test_pe_optimizer.py`(fake client):给若干"低分/转人工"用例 → 产出结构化改进建议列表;空 → 空列表;坏输出 → 跳过。
- [ ] **实现** `app/evaluation/pe_optimizer.py`:`collect_bad_cases(store)`(复用 reflow/trace)+ `propose_prompt_tweaks(...)`(LLM 归纳共性问题→给 prompt 增补建议)。
- [ ] 提交。

### Task H4.2 — 入口 + 与 eval 衔接
- [ ] **实现**:`app/scripts/run_pe_optimize.py` 打印改进建议;文档写明"人工评估→改 prompt→跑 eval 回归门禁验证不回退"的闭环。
- [ ] `settings.pe_optimize_enabled`(默认 False,离线手动)。
- [ ] 提交:`feat(eval): H4 PE 自动化——失败用例归纳出提示词改进候选`。

**验收**:能从失败用例产出可读的 prompt 改进建议,闭环接 eval 回归。**工作量**:~2 人日。

---

## 总量与顺序

H1(含 H1.0 领域改 + G1 选择器,~3.5d)→ H2(含 G2 结构化档案,~2.5d)→ H3(含 G3 自改进/用户建模闭环,~4d)→ H4(含 G4 标注采集,~2.5d),共 **~12.5 人日**。H1 最核心先做;H3 风险最高、全程半自动。每阶段独立可交付、可回退。

## Self-Review(写完自查 —— 覆盖两图 + G1–G4)

- **逐组件覆盖核对**(对照大图):
  - **架构收敛**:**H1.0-C 移除单 Agent 模式**,总控为唯一运行入口(`EcomAgent` 降为内部引擎,不删)。
  - **总控Agent**(React/记忆/业务权限/生命周期):能力已有但散落 → **H1.0 显式化为一处入口**(orchestrator 暴露 react/memory/permissions/lifecycle);领域子Agent→**H1.0 改售前/售中/售后**;功能型多Agent(出话/评估/润色)→**H1**;**选择器动态调度**→**H1.2/G1**。
  - Memory 长期(base profile/行为标签/工单流转)→**H2.3/G2**;Conversation 短期✅已有;记忆管理 SQLite WAL+FTS5→**H2**;上下文引擎✅已有。
  - Skill自动化生成(策划/创建/**自改进**/FTS5召回/**用户建模**/自述化循环)→**H3+H3.3/H3.4/H3.5(G3)**;Skills/Tools/MCP/HITL✅已有。
  - 数据飞轮 PE自动化→**H4**;**数据层标注**(意图/话术/正确性/润色)→**H4.0/G4**;RL🤝排除。
- **占位符扫描**:无 TBD;新模块均给接口签名/行为/来源;门控/接地/半自动策略明确。
- **一致性**:`ReplyPipeline.run`+`FunctionalSelector.choose`、`MemoryFtsStore`、`UserProfile`、`synthesize_skills/improve_skill/model_user`、`reply_labels/label_reply`、`propose_prompt_tweaks` 命名一致;开关 `*_enabled` 统一。
- **诚实标注的简化**:G1 **默认 LLM ReAct 选择器**(每步真 LLM 推理选下一个功能 Agent,100% 对齐图),规则选择器仅作兜底;G3 全程**半自动**(产候选人工确认,不自动改线上);G4 **半自动打标**(评估器 auto + 坐席 gold),非全人工标注平台。
- **风险点**:H3 合成/自改进质量(半自动缓解)、H1 复杂轮多次 LLM(分级门控+max_rounds 缓解)、FTS5 可用性(LIKE 降级)、G2 档案与现有 users/account 表同步一致性(单一写入口);**H1.0-C 移除单 Agent 后需清理 CLI/eval 的 single 模式引用**,直接构造 `EcomAgent` 的单元测试保持有效(测引擎)。

## Execution Handoff

方案已存 `docs/superpowers/plans/2026-07-24-harness-multi-agent-alignment.md`。审核通过后两种执行方式:
1. **Subagent-Driven(推荐)**:逐任务派 subagent 实现 + 复核。
2. **Inline**:本会话按阶段批量执行 + 检查点。
