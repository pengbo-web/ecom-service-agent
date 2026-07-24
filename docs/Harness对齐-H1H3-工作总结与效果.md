# Harness 架构对齐(H1–H3)工作总结与效果

> 本文记录按 `docs/superpowers/plans/2026-07-24-harness-multi-agent-alignment.md` 完成的三个阶段改造:
> **做了什么、为什么做、效果是什么、怎么验证的**。H4(标注采集 + PE 数据飞轮)暂缓,计划保留。
>
> 提交范围:`10cc17c..7962f4e`(20 个 commit,分支 `feature/w1-service-streaming`)。

---

## 0. 改造目标与总体架构

**目标**:把项目严格对齐行业电商客服 **Harness 架构**(得物式总控多 Agent 体系),使其具备生产级客服系统的完整形态——总控编排、两层 Agent 分工、记忆管理、结构化用户画像、技能自生长。全部**手写实现**(不引入 AutoGen/LangGraph),保留通用电商业务。

### 改造前 → 改造后

```
改造前                                改造后
─────────────────────                ─────────────────────────────────────────
单 Agent / 多 Agent 双模式并存         总控 Agent(MultiAgentOrchestrator)= 唯一运行架构
  (multi_agent_enabled 切换)           EcomAgent 降为总控驱动的内部 ReAct 引擎

领域路由:售前/售后/投诉               领域路由:售前/售中/售后(投诉并入售后)

ReAct 出话即最终回复                   出话草稿 → LLM 选择器动态调度 评估/重写/润色
                                        (功能型第二层 Agent,复杂轮才走,G1)

长期记忆全量注入 prompt                FTS5 全文索引,按当前问题关键词召回、命中优先注入

记忆 = 平铺事实列表                    + 结构化用户档案:会员信息/行为标签/工单流转(G2)

Skill 全人工编写                       Skill 自动化闭环:聚类合成/失败自改进/用户建模,
                                        半自动产候选,人工审核入库(G3)
```

### 两层 Agent 结构(对齐 Harness 图)

```
用户输入
   │
   ▼
总控 Agent(react / memory / permissions / lifecycle 四职责)
   │
   ├─ 第一层:领域路由 ──► 售前 / 售中 / 售后(各自 prompt + 工具子集,同一硬化引擎)
   │                          │
   │                          ▼  ReAct 工具循环 → 出话草稿
   │
   └─ 第二层:功能分工(仅复杂轮)
        LLM ReAct 选择器每步推理选下一个功能 Agent:
        评估(接地/准确/合规/完整)→ [不合格→重写→再评估] → 润色(小夕人设,事实锁定)→ done
```

---

## 1. Phase H1 — 功能型多 Agent(出话/评估/润色 + LLM ReAct 选择器 G1)

### 1.1 做了什么

| 提交 | 内容 |
|---|---|
| `10cc17c` H1.0-A | 领域改 **售前/presale、售中/midsale、售后/aftersale**;三域各配工具子集(售前:查商品/优惠/议价;售中:查单/物流/催发/改址/取消;售后:退款/发票/物流)。**工具隔离**=权限隔离:售前画像根本拿不到 `apply_refund` |
| `c72aeeb` H1.0-B | 总控 Agent 显式化四项职责入口:`react`(ReAct 引擎)、`memory`(记忆)、`permissions`(RISK_ACTIONS/consent/幂等/升级)、`lifecycle`(save/close/reset/状态),外加 `capabilities()` |
| `875590a` H1.0-C | **移除单 Agent 运行模式**:API/CLI 工厂恒建总控;`multi_agent_enabled` 废弃(恒当 True);EcomAgent 保留为内部引擎 |
| `b20f718` | 总控补 `skill_manager` 委托(恢复 CLI `/skills`);删除 235 行已失效的 legacy e2e 测试 |
| `859e47f` H1.1 | 三个提示词:`EVALUATOR_PROMPT`(四维度:**接地**/准确/合规/完整,只输出 JSON `{ok,issues,suggestion}`)、`REDRAFT_PROMPT`(据本轮工具真实结果重写,不编造)、`POLISH_PROMPT`(小夕人设,铁律:**金额/日期/状态/结论原样保留,不得改变任何事实**) |
| `48ff2a1` H1.2 | `ReplyPipeline` + `FunctionalSelector`:**选择器每步真用 LLM 推理**决定下一个功能 Agent(next ∈ evaluate/redraft/polish/done),可循环;LLM 失败/非法输出自动回退规则选择器;`reply_pipeline_max_rounds`(默认 2)防无限重写;评估器异常 **fail-open** 绝不阻断 |
| `795ce58` H1.3 | 接入 `EcomAgent.chat()`:`_grounding_context()` 提取"最近一条 user 之后的全部工具结果"(每条截 500 字)作为**接地上下文**;**分级门控**——只有复杂轮(`_step_seq>0`,本轮调过工具)才走流水线 |
| `50e911b` | 冒烟修复:真 LLM 选择器润色后反复返回 polish → 改为"润色即收敛 done" |

### 1.2 效果

**质量**:复杂轮的回复不再是 ReAct 草稿直出,而是经过——
1. **评估**:对照本轮工具真实结果检查是否接地(防"退款失败却说成功"这类谎报)、准确、合规、完整;
2. **重写**(仅评估不合格时):依据工具真实结果重写,不脱离事实;
3. **润色**:统一小夕客服人设语气,但金额/日期/订单状态等事实一字不动。

**成本**:三重控制——
- 简单轮("你好"/闲聊,没调工具)**零额外 LLM 调用**,直接用草稿;
- `max_rounds=2` 封顶重写循环;
- 润色即收敛(冒烟抓到的缺陷:修复前真 LLM 选择器每个复杂轮白烧约 10 次调用,修复后恰好 evaluate×1 + polish×1 + 选择器×3)。

**真 LLM 端到端冒烟证据**(总控唯一入口):
```
复杂轮"查订单 ORD-20240115-001":
  route → thought → tool_call → tool_result → select → evaluate → select → polish → select(done)
  回复接地真实订单数据(shipped/SF1234567890/预计到达)✅
简单轮"你好":
  route → thought(无 evaluate/polish,零额外成本)✅
```

**架构**:与 Harness 图"总控 ReAct 编排出话/评估/润色"100% 对齐——不是规则调度,而是**每步真 LLM 推理选择下一个功能 Agent**(规则选择器只作兜底)。

**回退**:`reply_pipeline_enabled=False` 一键回到改造前行为。

---

## 2. Phase H2 — FTS5 记忆管理 + 结构化用户档案(G2)

### 2.1 做了什么

| 提交 | 内容 |
|---|---|
| `5792b14` H2.1 | `MemoryFtsStore`:SQLite FTS5 全文索引存储层(运行时真探测,不支持自动降级普通表,**接口不变**);**中文召回鲁棒**——SQL 只按 user_id 过滤,Python 侧逐词子串计数打分(FTS5 默认分词不切中文,天真 MATCH 对中文几乎不可用;此法对任意长度中文关键词都有效,且天然无 SQL 注入);user 隔离在 SQL 层;top_k/去重 |
| `f6a8b39` H2.2 | 接入 `LongTermMemory`:`save()` 时**全量重同步索引**(fact_id=content 哈希;策展整体替换 facts 也不漂移);新增 `recall(query, top_k)`;**命中优先注入**——`build_prompt_section(query)` 把与当前问题相关的记忆排前并标注"与当前问题相关",其余记忆照旧跟随(**不丢任何记忆**);`chat._build_messages()` 自动取本轮用户输入作 query |
| `b3fb53c` H2.3 | **G2 结构化档案** `UserProfile`:`base`(会员等级/联系方式/姓名)+ `tags`(行为标签)+ `tickets`(工单流转);SQLite 双表(`user_profile`/`user_tickets`);`to_prompt()` 注入(全空返回 None,零影响);**HITL 升级自动落工单**(streaming.py escalate 点,`record_ticket` best-effort 绝不影响主流程);模块级单例 + 测试可复位 |
| `a80bab5` | 评审修复(Important):sqlite 连接跨 FastAPI 线程池线程抛 `ProgrammingError`,被 best-effort 吞掉后表现为**生产静默失效** → `check_same_thread=False` + `threading.Lock`,加修复前必红的跨线程回归测试 |
| `7962f4e` | 终审隐患修复:`recall_user_memory` 工具用模块级全局存 manager,多 agent 并存时**串户**(A 会话读到 B 的记忆)→ 改 ContextVar + `chat()` 每轮刷新(与 bargain 同模式),加串户回归测试 |

### 2.2 效果

**召回精准**:用户问"红色的鞋",与之相关的历史记忆("用户喜欢红色运动鞋")被 FTS 命中、**排前并标注**注入,而不是 50 条事实平铺——模型注意力更聚焦,长期记忆规模增长时 token 也可控。

**用户画像结构化**:小夕现在"认识"老客户的三个维度——
- **base**:钻石会员、联系方式(可从业务库 best-effort 同步);
- **tags**:行为标签(H3.4 用户建模自动写入,如"偏好红色/价格敏感");
- **tickets**:工单流转——用户曾转人工投诉过什么,自动记录、注入上下文,后续对话有连续性。

**生产可靠性**:两处评审/终审抓出的"在测试里全绿、上生产必坏"的缺陷(跨线程 sqlite、recall 串户)都已修复并有回归测试锁住。

**回退**:`memory_fts_enabled=False` 回全量注入;`memory_profile_enabled=False` 完全禁用档案(不建库/不注入/不落工单)。

---

## 3. Phase H3 — Skill 自动化生成闭环(G3,全程半自动)

### 3.1 做了什么

| 提交 | 内容 |
|---|---|
| `cc312ce` H3.1 | `synthesizer.py`:`group_samples`(归档会话按意图关键词粗聚类:退款/物流/发票/议价,先中先得确定性)→ `synthesize_one`(LLM 从同类样本归纳完整候选 SKILL.md,frontmatter 校验,坏输出跳过不崩)→ `synthesize_skills`(<2 条的组不合成;样本截断防 prompt 爆炸) |
| `2e72be8` H3.2 | 离线入口 `python -m app.scripts.synthesize_skills`:读 R5 冷归档(`list_recent_archives`)→ 产候选到 `definitions/_candidates/`;`skill_synth_enabled` **默认关**;已验证 SkillManager 不会误加载候选目录 |
| `86729f9` H3.3 | `improve_skill`:现 skill + 相关失败会话 → LLM 产**改进版候选**;无失败样本不改 |
| `fdffd27` H3.4 | `user_modeling.py::model_user`:LLM 从用户归档行为归纳**偏好标签**(JSON 数组,≤10 字×最多 5 个)→ 写入 G2 档案 `tags`;空样本零调用;坏输出返回空不崩 |
| `5b791da` | 评审修复(Important):LLM 意外改名会让改进候选写到漂移目录、甚至**静默覆盖无关候选** → 强制 name 一致性,改名按坏输出丢弃 + "不误覆盖 victim"回归测试 |
| `8dde199` H3.5 | 入口扩为**三步自述化闭环**:①用户建模(写档案标签)②聚类创建候选 ③对已入库 skill 失败自改进(`is_failure_sample` 启发式:转人工/连环道歉/投诉摘要;`related_failures` 关键词关联);三步 try/except 隔离 fail-soft,一步失败不影响其余 |

### 3.2 效果

**系统能从真实会话里"自己长技能"**:
- 重复出现的客服模式 → 候选 SKILL.md(策划/创建);
- 失败会话(转人工/投诉)→ 已有技能的改进稿(自改进);
- 用户行为 → 画像标签(用户建模,与 H2 档案注入联动)。

**真 LLM 闭环冒烟证据**(4 条造样归档):
```
① 用户建模:u1 → [偏好红色, 常问物流]  u2 → [退款敏感, 流程厌恶, 投诉倾向]
   → 已写入档案库,下次对话自动注入 ✅
② 聚类创建:合成出候选 complaint-refund-process(frontmatter 合法可解析)✅
③ 失败自改进:对真实已入库 process-return 产出改进候选 ✅
```

**半自动铁律**(风险最高的阶段用流程兜底):一切 skill 产物只进 `_candidates/`,**人工审核后手动移入 `definitions/` 才生效**;入口开关默认关,离线手动跑;标签直写档案(低风险,且画像本身就注入可见、可查)。

---

## 4. 过程中抓住并修复的关键缺陷

单任务评审 + 整分支终审 + 真 LLM 冒烟,共抓出 3 个 Important + 2 个终审项,全部修复并有回归测试:

| 缺陷 | 怎么发现 | 危害 | 修复 |
|---|---|---|---|
| LLM 选择器润色后反复 polish 不收敛 | 真 LLM 冒烟(脚本化测试掩盖不了) | 每个复杂轮白烧 ~10 次 LLM 调用 | 润色即收敛 done(`50e911b`) |
| sqlite 连接跨线程 ProgrammingError | 任务评审(真实线程复现) | 生产环境工单/注入/召回**静默失效** | `check_same_thread=False`+锁(`a80bab5`) |
| improve_skill 改名导致候选漂移/覆盖 | 任务评审 | 改进结果丢失、无关候选被静默覆盖 | 强制 name 一致(`5b791da`) |
| sandbox 用已删除的 `.agents` 属性 | 整分支终审 | 离线 eval 时画像 ToolManager 不被关闭 | 改用 `.profiles`(`c33b558`) |
| recall_user_memory 全局单例串户 | 整分支终审(pre-existing) | 多会话并存时 A 用户读到 B 的记忆 | ContextVar+每轮刷新(`7962f4e`) |

> 经验:**脚本化测试证明逻辑,真 LLM 冒烟证明行为**——选择器不收敛这类缺陷只有真模型能暴露。

---

## 5. 开关清单(全部可独立关闭、关即回退改造前行为)

| 开关 | 默认 | 作用 |
|---|---|---|
| `reply_pipeline_enabled` | True | H1 评估/重写/润色流水线 |
| `reply_pipeline_max_rounds` | 2 | 评估-重写循环上限 |
| `selector_mode` | `llm` | 功能选择器:`llm`(每步真推理)/`rule`(规则) |
| `memory_fts_enabled` | True | H2 FTS 召回 + 命中优先注入(关=全量注入) |
| `memory_profile_enabled` | True | G2 结构化档案(关=不建库/不注入/不落工单) |
| `skill_synth_enabled` | **False** | H3 离线合成入口(高风险能力,离线手动) |

测试环境:`tests/conftest.py` 全局关闭 `reply_pipeline_enabled` 保证既有语料确定性,需要的测试显式开启;生产 on 路径由真 LLM 冒烟覆盖。

## 6. 如何体验/验证

```bash
# 离线测试(核心子集,不触网)
.venv/Scripts/python.exe -m pytest tests/test_reply_pipeline.py tests/test_chat_pipeline_wiring.py \
  tests/test_memory_fts.py tests/test_memory_profile.py tests/test_skill_synth.py \
  tests/test_user_modeling.py tests/test_synth_loop.py tests/test_controller_agent.py \
  tests/test_orchestrator_unified.py tests/test_react_degrade.py tests/test_memory_tool_isolation.py -q
# → 96 passed

# H1 体验:起服务后问"查订单 ORD-20240115-001"(复杂轮)
#   事件流出现 select/evaluate/polish;问"你好"则不出现(门控)
# H2 体验:多轮对话让系统记住偏好,换相关话题提问,观察注入的"与当前问题相关的记忆"排前
# H3 体验:.env 设 skill_synth_enabled=True 后
python -m app.scripts.synthesize_skills 50
#   → definitions/_candidates/ 出现候选;档案库 tags 更新;人工审核后移入 definitions/ 生效
```

## 7. 遗留与后续

- **H4 暂缓**(用户决定):G4 标注采集(评估器 auto 打标 + HITL gold 标)+ PE 失败归纳出提示词改进建议 + eval 回归门禁衔接。计划文档已备,随时可续。
- 终审 triage 为"可留"的 Minor:select 事件 reason 用诊断标签(可观测性小折扣)、`_tokenize` 对无分隔长句召回率偏低(换分词器需新依赖,暂不引)、top_k=5 硬编码、`test_agent.py::test_reset` 旧属性(pre-existing 网络 demo)。
- 详细执行账本:`.superpowers/sdd/progress.md`;实现方案:`docs/superpowers/plans/2026-07-24-harness-multi-agent-alignment.md`。
