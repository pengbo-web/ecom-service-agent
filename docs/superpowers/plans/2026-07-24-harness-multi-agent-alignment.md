# Harness 多 Agent 架构对齐 实现方案(H1–H4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 把项目对齐行业电商客服 **Harness 架构**——在已有"领域路由"之上补齐"功能分工(出话/评估/润色)"、FTS5 记忆管理、Skill 自动化生成、PE 自动化数据飞轮(RL 除外),全部**手写**、保留**通用电商**业务。

**Architecture:** 两层 Agent = 第一层领域路由(已有:售前/售后/投诉)+ 第二层功能分工(新增:总控内 出话→评估→润色 的 ReAct 编排,复用同一硬化引擎)。外围叠加 Harness 能力层:双层记忆(已有)+ FTS5 记忆管理(新)+ 上下文引擎(已有)+ Skills 管理(已有)+ Skill 自动生成(新)+ Tools/MCP(已有)+ HITL(已有)+ 数据飞轮 PE 自动化(新)。

**Tech Stack:** Python 3.11、现有 EcomAgent ReAct 引擎、SQLite FTS5(内置)、Redis(会话存储,已有)、OpenAI 兼容 LLM;测试用 fake client / fakeredis / 临时目录,不触网。

## Global Constraints

- **手写实现,不引入 AutoGen / LangGraph 等重依赖**;对齐架构形态而非厂商。
- **保留通用电商业务**,不新建得物式尺码/spuid 工具与数据。
- **分级门控**:功能层(评估/润色)仅"复杂轮"(本轮调用过工具)全走,简单轮直接用草稿,省 LLM 成本。
- **每个新能力带 `settings` 开关,默认开、可关**;关闭即回退到改造前行为。
- **接地铁律**:评估/重写必须带本轮工具真实结果,重写不脱离事实;评估器异常 **fail-open**(不阻断回复)。
- **向后兼容**:记忆/存储改动兼容旧 JSON / 旧库;老会话可读。
- 每阶段:**独立提交 + 离线全量绿 + 端到端冒烟(真 Redis / fakeredis)+ commit 带阶段号**。
- **不做 Agentic RL 训练**(数据飞轮只做 PE 自动化侧)。

## File Structure

| 文件 | 阶段 | 责任 |
|---|---|---|
| `app/prompts/reply_pipeline.py`(新) | H1 | 评估器 / 重写 / 润色 提示词 |
| `app/agent/reply_pipeline.py`(新) | H1 | 出话→评估→(重写)→润色 手写编排 |
| `app/agent/chat.py`(改) | H1 | ReAct 产出草稿后接入流水线 + 接地上下文提取 |
| `app/config/settings.py`(改) | H1-H4 | 各能力开关 |
| `app/agent/memory/fts_store.py`(新) | H2 | SQLite FTS5 记忆全文索引:写入 / 关键词召回 |
| `app/agent/memory/long_term.py`(改) | H2 | 事实写入/召回接 FTS5(与现有 LLM 策展并存) |
| `app/agent/skills/synthesizer.py`(新) | H3 | 从归档会话聚类 → LLM 生成候选 skill markdown |
| `app/scripts/synthesize_skills.py`(新) | H3 | 离线生成入口(半自动:产候选,人工审核入库) |
| `app/evaluation/pe_optimizer.py`(新) | H4 | 从标注数据产出提示词改进候选(供人工采纳) |
| `tests/test_reply_pipeline.py` / `test_memory_fts.py` / `test_skill_synth.py` / `test_pe_optimizer.py`(新) | H1-H4 | 各阶段离线测试(fake client) |

---

## Phase H1 — 功能型多 Agent(出话 / 评估器 / 润色)

**Interfaces:**
- Consumes:现有 `EcomAgent._react_loop()` 的 `final_text`(= 出话草稿)、`self._step_seq`(>0 表本轮用过工具 = 复杂轮)、`self.raw_messages`(取本轮 tool 结果做接地)。
- Produces:`ReplyPipeline.run(client, model, user_input, draft, grounding, complex_turn, emit) -> str`(最终回复);发 `evaluate`/`polish` 事件供 trace。

### Task H1.1 — 提示词(评估/重写/润色)
- [ ] **写测试**:`tests/test_reply_pipeline.py` 断言三提示词非空且含关键约束词("接地"/"不得改变任何事实")。
- [ ] **实现** `app/prompts/reply_pipeline.py`:`EVALUATOR_PROMPT`(四维度:接地/准确/合规/完整,输出 JSON `{ok,issues,suggestion}`)、`REDRAFT_PROMPT`(据工具真实结果重写、不编造)、`POLISH_PROMPT`(小夕人设,铁律:金额/日期/状态/结论原样保留)。
- [ ] 运行:`.venv/Scripts/python.exe -m pytest tests/test_reply_pipeline.py -q` → 提示词测试通过。
- [ ] 提交。

### Task H1.2 — ReplyPipeline 编排(手写)
- [ ] **写测试**(fake client 脚本化):
  - 简单轮(`complex_turn=False`)→ 原样返回草稿(门控)。
  - 复杂轮 + 评估 `ok=true` → 只润色(草稿→润色文本)。
  - 复杂轮 + 评估 `ok=false` → 重写→润色。
  - `reply_pipeline_enabled=False` → 原样返回草稿。
  - 评估返回坏 JSON → fail-open(当作 ok,继续润色,不抛错)。
- [ ] **实现** `app/agent/reply_pipeline.py`:`ReplyPipeline.run(...)` 按"门控→评估→(重写)→润色";`_chat/_evaluate/_redraft/_polish` 各一次 LLM 调用;评估解析失败 fail-open;重写/润色异常返回空回退到上一版。
- [ ] 运行该测试文件绿。提交。

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

**验收**:记忆按 query 关键词精准召回、按 user 隔离;记忆多时不再全量塞 prompt。**工作量**:~1.5 人日。

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

**验收**:能从真实归档会话产出结构合法的候选 skill,人工审核即可入库。**工作量**:~3 人日(风险:合成质量靠 prompt 调,故做半自动)。

---

## Phase H4 — PE 自动化(数据飞轮)

> 从标注数据(回复正确性标注 / 优秀客服润色数据 / 转人工原因)离线产出**提示词改进候选**,供人工采纳。轻量、与现有 eval 回归门禁衔接;**不自动改线上 prompt**。

**Interfaces:**
- Consumes:eval 失败用例 / `session_archive` / trace 的低分会话。
- Produces:`propose_prompt_tweaks(client, model, bad_cases) -> list[str]`(改进建议)。

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

H1(2.5d)→ H2(1.5d)→ H3(3d)→ H4(2d),共 **~9 人日**。H1 最核心先做;H3 风险最高、做半自动。每阶段独立可交付、可回退。

## Self-Review(写完自查)

- **覆盖**:两图的功能型多Agent(H1)、FTS5 记忆管理(H2)、Skill 自动生成(H3)、PE 自动化数据飞轮(H4)均有任务;RL 明确排除;领域路由/双层记忆/上下文压缩/Tools/HITL/consent/生命周期已存在,不重复。
- **占位符扫描**:无 TBD;关键新模块给了接口签名与行为;提示词/门控/接地规则明确。
- **一致性**:`ReplyPipeline.run` 签名、`MemoryFtsStore.index/search`、`synthesize_skills`、`propose_prompt_tweaks` 在文内一致;开关命名 `*_enabled` 统一。
- **风险点**:H3 合成质量、H1 每轮 3x LLM(已用分级门控缓解)、FTS5 可用性(已给 LIKE 降级)。

## Execution Handoff

方案已存 `docs/superpowers/plans/2026-07-24-harness-multi-agent-alignment.md`。审核通过后两种执行方式:
1. **Subagent-Driven(推荐)**:逐任务派 subagent 实现 + 复核。
2. **Inline**:本会话按阶段批量执行 + 检查点。
