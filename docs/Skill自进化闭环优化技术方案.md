# Skill 自进化闭环优化技术方案(参考图对齐版)

> 日期:2026-09-21　状态:提案(未实施)
> 上位文档:`docs/架构对比-参考架构与本项目现状.md`(§3.3 / §8 附录 A)、`docs/自进化Skill技术文档.md`
> 范围:仅 `app/agent/skills/` 自进化闭环及其供给链;不动运行回路热路径语义、不拆治理层。
> **不含 Agentic RL 训练飞轮**(参考图右挂耳)——2026-09-21 用户决策明确不做,理由见 §6 非目标。

---

## 0. 背景、目标与三条原则

### 0.1 现状一句话

本项目的自进化闭环已经是"**带刹车的闭环**":归因过滤(attribution)→ 候选合成(只写 `_candidates/`)→ 零 LLM 裁判尺(case_synthesis)→ 离线门禁(gate,fail-closed)→ 五道闸转正(promote)→ 会话哈希灰度(canary)→ 看门狗收口(watchdog)。参考图(鞋服垂直域客服架构)给出的是另一种长法:**Skill 自动化生成盒**(skill 记忆 / 进化循环 / 用户建模 / FTS5 召回 / 创建 Skill / 失败自进化)+ **双数据飞轮**(PE 自动化 / Agentic RL)。

### 0.2 参考图里真正值得借的三个点

对照附录 A 的结论,参考图六块中本项目"块块有对应",但有三件事它画了而我们没有、或做得不够:

1. **skill 记忆的"在线感知"**:参考图让 Agent 自主判断"什么值得记忆"。我们的判断权在离线归因(刻意,有 41 条实证背书),但**在线侧完全没有"这一轮似乎值得学"的感知通道**——归因的输入只有 outcome=tool_error/handoff 的轨迹,一类重要信号进不来:**买家纠正了客服、客服答得自洽但答错了、KB 没命中却硬答了政策**。这类轮次 outcome 往往是 success,归因永远看不到。
2. **FTS5 全文召回作为生成侧采样源**:我们的采样只有轨迹(强)+ 关键词(弱);新蒸馏候选零轨迹零关键词命中时采样面很窄。
3. **进化循环的可见性**:参考图有一个居中的"进化循环"符号;我们的三回路状态散在 CLI 输出里,没有 per-skill 的循环状态视图。

(参考图的第四个特征 Agentic RL 飞轮经决策**不纳入本方案**,见 §6。)

### 0.3 三条原则(本方案所有设计的约束)

- **借 shape 不借 brake**:参考图的块命名与循环符号可以借;五道闸/灰度/回滚/口径纪律一处不删。任何新来源、新信号进入闭环,先穿过既有治理。
- **判断权不上线**:参考图的"Agent 在线自主判断值得记忆"不照抄。在线侧只做**标记**(零决策、零回流),判定权仍归离线规则表。这是 41 条归属拦截实证换来的纪律。
- **新源先过口径**:任何新数据进采样/判定前必须带流量来源;`simulated`/`eval`/`loadtest` 永不进 DECISION 与 SAMPLING 两套口径(防"学自己的回声"与闭环自证)。

### 0.4 目标(可验收)

| # | 目标 | 验收口径 |
|---|---|---|
| G1 | 成功轮次的"值得学"信号可采集、可报数 | 知识缺口类轨迹中带在线 hint 的比例 ≥ 60%;hint 表有 source 分布报表 |
| G2 | 采样面扩大且纪律不破 | 零轨迹候选的 evaluable 技能数 +≥2;合成用例来源报表出现第三源且强弱分报 |
| G3 | 进化循环 per-skill 可见 | 一个命令/一个接口看全 7 个现行 + 全部候选的循环状态;NEEDS_HUMAN 项零遗漏 |
| G4 | 多轮评测破零 | ≥5 条多轮种子用例 + 用户模拟器原型跑通,流量标 SOURCE_SIMULATED |

---

## 1. 目标架构总览(优化后)

```
① 运行回路(不变,仅旁路加标记)
   matcher → loader → workflow守卫 → 工具执行 → skill_traces
                                        └→ [新] skill_memory_hints(在线标记,零决策)
                                            来源:用户纠正(规则) / KB miss+政策意图(规则)
                                                 / 会话末 LTM 抽取捎带字段(开关默认关)
② 离线回路(增强)
   轨迹+hints → attribution(规则表仍为唯一判定权;hints 只影响采样优先级与报数)
   归档会话 ──→ clustering 语义聚类
              ├→ [新] archive_fts 全文召回(第三采样源,弱,分报)
              └→ case_synthesis / golden_corpus 采样(SOURCE_TRACE 强 / SOURCE_KEYWORD 弱 / SOURCE_FTS 弱)
   user_modeling → profile(不变);[新] 群体标签统计 → 循环看板(只报不回流)
③ 放行回路(一字不改)
   gate → promote 五道闸 → canary → watchdog → _archive
④ [新] 循环看板 skill_loop_status:per-skill 聚合 ①②③ 全部状态 + NEEDS_HUMAN
```

---

## 2. WS1 · Skill 记忆两层化:在线标记 + 离线判定(对齐参考图"skill 记忆"块)

### 2.1 问题

归因的输入面是 `outcome ∈ {tool_error, handoff}` 的轨迹(`attribution.py` 批量入口)。但三类"值得学"的信号发生在 **success 轮**:

- **用户纠正**:下一轮买家说"不对,我问的是…/你说错了"——本轮客服答错但答得自洽;
- **无依据硬答政策**:QU 判 `need_kb=True`、召回 outcome=miss/degraded,模型仍然给出了政策口径(commitment_guard 只观测金钱承诺,覆盖不了全部政策陈述);
- **人工救场的前兆**:会话末被接管(已有规则⑥覆盖),但**接管前那几轮的错误答案**目前没有标记,改进样本定位不到具体轮。

参考图用"Agent 自主判断什么值得记忆"覆盖这类信号;我们用"**在线零决策标记 + 离线规则判定**"覆盖,保住判断权纪律。

### 2.2 设计

**新表**(ecom.db,与 skill_traces 同库不同表,避免轨迹 schema 漂移):

```sql
CREATE TABLE IF NOT EXISTS skill_memory_hints (
    hint_id     TEXT PRIMARY KEY,          -- md5(session:turn:kind)[:12]
    session_id  TEXT NOT NULL,
    skill_name  TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,             -- user_correction / kb_miss_policy / piggyback_note
    source      TEXT NOT NULL,             -- rule / llm_piggyback
    traffic     TEXT NOT NULL,             -- 落库时 get_traffic_source(),口径纪律同源
    detail      TEXT NOT NULL DEFAULT '',  -- 规则命中词 / 捎带笔记原文(截 200 字)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hints_skill ON skill_memory_hints(skill_name, kind);
```

**三个生产者**(前两个零 LLM、零额外延迟):

1. `user_correction`(规则):在 `chat.py` 轮末埋点处,对**本轮用户输入**跑纠正模式表(整句锚定 + 否定词:`不对|说错了|不是这个意思|我问的是|你搞错了`,≤20 字门限,取材纪律同 understanding 扩展规则表"宁漏勿错杀");命中则给**上一轮**该 session 的 skill 记 hint。模式表放 `escalation.py` 同风格的常量模块,判据跟真源走。
2. `kb_miss_policy`(规则):QU `need_kb=True` ∧ 本轮 recall outcome ∈ {miss, degraded} ∧ intent ∈ 政策类集合(复用 `_QU_INTENT_MAP` 的政策分支)→ hint。判据全部是本轮已有结构化字段,零新调用。
3. `piggyback_note`(LLM 捎带,**开关默认关** `skill_hint_piggyback_enabled=False`):会话末 `consolidate_to_long_term` 的 LTM 抽取 JSON 增加可选字段 `skill_gap_note`(≤80 字);"捎带"意味着**零额外 round trip**。解析失败/缺字段 → 无 hint,fail-soft。开关打开前先跑一周离线复算比对规则源重合度。

**消费者(只两处,都不做判定)**:

- `failure_cases` / `improve_skill` 的失败样本排序:同分时带 hint 的轨迹优先(hint 是**优先级**,不是资格);
- `skill_loop_status`(WS3)报 `hints_count` 按 kind/source 分布。

**判定权不变的红线**:attribution 七条规则顺序与类别定义一字不改;hint **不参与**类别判定。排序保证:即使某轮同时有 hint 与归属拒绝特征,规则②仍先命中 capability_limit,hint 在该轮惰性化(写进表但采样不优先)——在方案里写明这条顺序论证,防未来有人把 hint 提进判据。

**fail 方向**:hint 写入 fail-soft(异常吞掉,埋点纪律同 skill_traces);hint 表读不到 → 采样排序退回现状;`traffic` 非 live 的 hint 在采样侧按 SAMPLING 口径过滤(与语料同纪律)。

### 2.3 改动清单

| 文件 | 改动 |
|---|---|
| `app/agent/skills/memory_hints.py`(新) | 表 DDL、`record_hint()`、`hints_for_skill()`、纠正模式表常量 |
| `app/agent/chat.py` | 轮末埋点段(chat.py:415-419 旁)加两处规则判定 + 写 hint;整段包既有 try/except |
| `app/agent/memory/extraction.py` | LTM 抽取 prompt/解析增加可选 `skill_gap_note`(门控开关) |
| `app/agent/skills/failure_cases.py` / `synthesizer.improve_skill` | 采样排序接入 hint 优先级 |
| `tests/test_memory_hints.py`(新) | 模式表宁漏勿错杀用例、归属拒绝轮 hint 惰性化、traffic 过滤、fail-soft |

---

## 3. WS2 · FTS5 会话全文召回接入生成侧采样(对齐参考图"FTS5 召回"块)

### 3.1 问题与纪律

现状采样两源:`SOURCE_TRACE`(强)/ `SOURCE_KEYWORD`(弱)。零轨迹的新蒸馏候选只有关键词路,而关键词来自 frontmatter 自报,召回面窄且受声明质量影响(实测三种声明写法坑)。参考图在生成盒内放 FTS5 召回,方向可借,**但借法受限**:全文召回是模糊匹配,只配做**采样源**,不配碰断言——"采样可以启发式,裁判尺不能"(`case_synthesis.py:20-33`);且卖家侧仍只走轨迹路(跨 actor 误捞实证,`case_synthesis.py:350-367`)。

### 3.2 设计

**新模块 `app/agent/skills/archive_fts.py`**:

- 对 `session_archive` 建 FTS5 虚表 `archive_fts_seg(user_id, session_id, content, raw UNINDEXED)`,content = 首条 user 消息 + 会话 summary 的 jieba 预分词(**复用 `memory_store._segment` 同款机器**,抽公共 util `app/utils/zh_segment.py` 防两套分词漂移);
- 索引带 `segmenter_version` 戳,版本不符 → 后台重建;重建失败 → 该源报 `unavailable`,**不回落静默空**(三态纪律同 faq_cache);
- API:`search_sessions(query, top_k=20, actor="buyer") -> list[(session_id, score)]`,actor 过滤在 SQL 层硬做(卖家侧调用直接返回空 + reason,纪律代码化);
- 写入时机:归档写入旁路增量索引(fail-soft,索引滞后只影响采样面不影响正确性)。

**接入点**:`case_synthesis` 增加 `SOURCE_FTS`(弱,provenance 记录),与 keyword 源并列、**分别报数**;`golden_corpus` 的候选会话发现可复用同一 API。断言生成逻辑零改动(turns/expected_tools/expected_keywords 的事实来源不变)。

**fail 方向**:FTS 不可用 → 该源跳过并报 unavailable(退回两源现状);坏索引 → 重建;查询异常 → 空 + warning。

### 3.3 改动清单

`app/utils/zh_segment.py`(新,抽公共)、`app/agent/skills/archive_fts.py`(新)、`case_synthesis.py`(加源+报数)、`golden_corpus.py`(可选复用)、`tests/test_archive_fts.py`(新:召回下限、actor 隔离、三态、provenance)。

---

## 4. WS3 · 进化循环看板:把"进化循环"变成可见对象(对齐参考图居中循环符号)

### 4.1 设计

**新 CLI + 管理接口**:`python -m app.scripts.skill_loop_status [--skill NAME] [--json]` 与 `GET /api/admin/skills/loop`。**纯只读聚合**,不新增写入、不改任何判定:

每个 skill 输出一行循环状态机:

```
track-order:
  运行: live 轨迹 17 轮 · 成功率 88%(live 口径) · hints 3(user_correction 2 / kb_miss 1)
  归因: knowledge_gap 0 · capability_limit 12 · noise 2 · undetermined 0
  候选: _candidates 1 份(fingerprint ab12…) · 风险档 low
  裁判尺: 用例 6(人工 1 + 合成 5) · evaluable=true · underpowered=false
  放行: 门禁未跑 · 灰度无活跃 · watchdog 上次=wait(样本不足)
  需人工: []            ← NEEDS_HUMAN_ACTIONS 逐 skill 展开,非零即告警
```

实现=聚合既有纯函数:`watchdog.evaluate_*`(纯函数不碰 DB)、`gate.gate_readiness`、`attribution.summarize`、`risk.promotion_policy`、hints 计数、canary 活跃表。**看板自己不做任何判定**,只渲染——防止"看板口径"与"执行口径"分叉(本仓库 drift 纪律)。

### 4.2 改动清单

`app/scripts/skill_loop_status.py`(新)、`app/api/` 管理路由一处、`tests/test_skill_loop_status.py`(口径与执行函数同源断言)。

---

## 5. WS4 · 用户建模:群体信号只报不回流(对齐参考图"用户建模"在 skill 盒内的位置,但守住挂载点分歧)

**保持现状**:个体偏好标签进 `UserProfile.tags`(`user_modeling.py`),**不进全局 skill**——偏好逐用户、skill 全局,混入即个体记忆污染公共政策(附录 A.2 块 3)。

**新增(只报)**:WS3 看板加一段群体统计(确定性计数,零 LLM):标签 cohort × 该 cohort 失败率/转人工率的对照表(样本 < `anomaly_min_samples=5` 显示 None 不显示 0)。用途是**给人看**"哪类人群的失败集中",是否据此改 skill 由人决策、走既有闭环。**明确非目标**:不做 cohort → 自动 skill 改写的回流通道。

---

## 6. 非目标:Agentic RL 训练飞轮(明确不做)

参考图右挂耳是"数据飞轮(Agentic RL 训练)"。本方案**不纳入**,决策记录(2026-09-21):

1. **收益与前提不匹配**:RL 飞轮的前提是有厚度的 reward 信号与大规模在线/离线交互数据;本项目实测语料仍薄(live 口径技能调用数百轮、人工评测 10 条),在信号质量尚未堆够之前上训练回路,等于在噪声上拟合。本项目一贯的论证是"数据薄时先做信号质量,不堆数据量",RL 与该论证相悖。
2. **治理成本不对等**:微调模型一旦进入生成/执行链路,五道闸的"候选是 Markdown 文档"这一前提被破坏(模型权重不可 diff、不可事实锚点比对、不可一眼审读),等于把最严的那道闸改造成最看不懂的闸。若未来重启 RL,唯一可接受的接入姿势是"微调模型只作候选生成器、产物仍是 SKILL.md、仍过五道闸",但该姿势的收益主体其实是生成器多样性,而非 RL 本身。
3. **替代物已覆盖其动机**:参考图 RL 飞轮想解决的"系统越用越好",在本项目由自进化闭环(技能层迭代)+ 评估回归门禁(选择压力)承担;技能层迭代的可审计性、可回滚性严格优于权重层迭代。

**若未来条件变化**(live 语料上一个量级、多轮评测成型、出现权重层才能表达的收益),重启 RL 立项时必须满足:reward 口径以规则格为主、流量口径纪律不变、产物只以候选形式进闭环。本节即该立项的前置约束清单。

---

## 7. WS5 · 多轮评测与用户模拟器(两架构共同缺口)

- **种子**:人工写 5 条多轮用例(含论文型失败模式:"第一轮说反规则但答案自洽、用户不放弃"),进 `cases.json`(人工集);
- **模拟器原型**:scripted persona 模板 + LLM 用户模型(停止条件:达成目标/放弃/超 N 轮),跑沙箱;流量标 `SOURCE_SIMULATED`——**两侧口径都排除**(纪律现成,`runtime_context.py:129-133`),它只服务评测,永不进语料采样;
- **配额**:估算 ~2500 次调用,需单独预算审批(现卡 `collab_daily_llm_budget=200` 口径),先申请独立 `eval_sim_budget`。

---

## 8. 治理护栏与 fail 方向总表(本方案新增组件全列)

| 新组件 | fail 方向 | 理由 |
|---|---|---|
| hint 写入(三生产者) | fail-soft 吞异常 | 运行回路旁路,绝不影响回话 |
| hint 读取/排序 | 缺失退回现状 | 增强不是依赖 |
| piggyback 解析 | 缺字段=无 hint | 不编信号 |
| archive_fts 建索引/查询 | unavailable 三态报出,不静默空 | 静默失败比失败更糟 |
| archive_fts 卖家侧调用 | 硬返回空 + reason | 跨 actor 纪律代码化 |
| loop 看板 | 只读聚合,单 skill 聚合异常该 skill 显示 error 不拖全表 | 看板不判定 |
| 模拟器流量 | SOURCE_SIMULATED 双口径排除 | 防闭环自证 |

**不变量测试(仓库级,新增断言)**:① hint 不参与 attribution 类别判定(给规则表喂带 hint 的归属拒绝样本,类别仍 capability_limit);② `synth-` 前缀与 cases 物理分离不变;③ 看板数值与执行函数同源(同一输入两路输出一致);④ 模拟器/沙箱流量永不出现于 SAMPLING/DECISION 集合。

---

## 9. 里程碑

| 里程碑 | 内容 | 工期 | 验收 |
|---|---|---|---|
| M1 | WS3 看板 + WS1 两个规则 hint 源 + 不变量测试 | 1 周 | **已完成 2026-09-22**:`memory_hints.py`(纠正模式表 + kb_miss_policy + 幂等落库 + 双形状读取口)、chat 旁路接线、`skill_loop_status.py`(六段状态 + attention + `--json`)、新增测试 25 条;全量回归 2838 过,8 败/8 收集错误均为环境缺依赖(redis/fakeredis/mcp 新版 API,会话前既存,与本轮无关);G3 达成(真实库冒烟:track-order live 17 轮 88.2% vs 全口径 51 条 tool_error 并排可见) |
| M2 | WS2 FTS 采样源 + WS1 piggyback 开关(先离线复算一周再开) | 1–2 周 | **已完成 2026-09-22**:`zh_segment.py` 分词收口、`archive_fts.py`(版本戳重建/增量旁路/三态/卖家硬隔离/子串保底)、归档旁路挂索引、`SOURCE_FTS` 第三采样源(首 user 轮对齐索引面、去重、stats 三源分报)、CLI 三源报数、piggyback 开关链(默认关);新增+波及测试 117 过,全量 2857 过(8 败为既存环境缺依赖);**实测坑**:jieba 索引侧/查询侧词表不对称致带引号 MATCH 恒空,已按 memory_store 同款子串保底修复。**备注**:现网归档的 FTS 命中会话均已被关键词源先取(去重正确),from_fts 增量需等新归档积累,piggyback 开关按方案离线复算一周后再开 |
| M3 | WS5 种子用例 + 模拟器原型(预算审批后)+ WS4 群体统计 | 2–4 周 | **已完成 2026-09-22(原型范围)**:cases.json 10→15 条(5 条多轮人工种子,含论文型"施压轮持守政策"模式);`user_simulator.py` scripted persona 原型(零 LLM、四确定性停止条件、SOURCE_SIMULATED 双口径排除、复用 Sandbox 隔离)+ 3 persona + `run_user_sim.py`;`cohort_stats.py` 群体标签×失败率只报不回流 + 看板群体段;新增测试 16 条,隔离全量 2873 过(8 败为既存环境缺依赖)。**备注**:LLM 驱动 persona 与模拟跑批入评估门禁待独立预算审批(~2500 次调用口径);期间发现全量测试与别的命令并发跑会出假失败(webui UnicodeDecodeError + 收集错误 8→39),回归须隔离跑 |

**风险**:① hint 噪声 → 先只报数两周再看是否接排序;② FTS 索引构建成本 → 增量旁路 + 后台重建;③ 模拟器配额 → 独立预算审批前不动工;④ 叙事风险(对外讲"参考了参考图")→ 统一口径:"借鉴其循环形态,判断权与上线治理保持本项目设计;RL 飞轮经评估不做",附 41 条实证与 §6 论证作论据。

---

## 附:参考图块 → 本方案工作项映射

| 参考图块 | 本方案 | 借法 |
|---|---|---|
| skill 记忆(Agent 自主判断) | WS1 两层化 | 借"在线感知",不借"在线判定" |
| 进化循环(居中符号) | WS3 看板 | 借可见性 |
| 用户建模 | WS4 | 借群体视角,守住"个体偏好不进全局 skill" |
| FTS5 召回 | WS2 | 借召回机器,限定为弱采样源 |
| 创建 Skill / 失败自进化 | 现状已覆盖 | 不改 |
| 数据飞轮 PE 自动化 | 现状闭环 + WS1/WS2 供料增强 | 不改治理 |
| 数据飞轮 Agentic RL | **不采纳**(§6 非目标,2026-09-21 决策) | 若未来重启,前置约束见 §6 末段 |
