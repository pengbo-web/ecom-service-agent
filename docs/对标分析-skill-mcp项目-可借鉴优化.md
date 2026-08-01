# 对标分析:ecommerce-customer-service-skill-mcp → 本项目可借鉴优化点

> 对标对象:`jlm191701/ecommerce-customer-service-skill-mcp`(下称 **Skill-MCP**),FastAPI + React + 独立 MCP,工程演示级电商客服参考实现。
> 本项目:`ecom-service-agent`(下称 **本项目**),多智能体 + ReAct + 记忆/RAG/技能/确认流,已接入真实黑马点评(hmdp)作数据源。
> 结论一句话:**本项目在"Agent 能力深度"上更强(议价确定性引擎、风险动作确认流、出话接地流水线、真鉴权+会话生命周期、真实外部系统集成、可观测);Skill-MCP 在"工程严谨度/生产纪律"上更强(规格+三级门禁、确定性安全评测、安全数据导入框架、可审计契约)。最该借鉴的正是后者这几块。**

---

## 一、两个项目定位

| | 本项目 ecom-service-agent | Skill-MCP |
|---|---|---|
| 智能承载 | 多智能体画像(售前/售中/售后 = prompt + 工具白名单)+ 统一 ReAct 引擎 | 单主 Skill(声明式 Markdown 文件夹包)+ 确定性编排 |
| 业务数据 | 真实接了 hmdp(Java+MySQL+Redis)via MCP;另有 mock/SQLite | mock / mysql(合成种子)/ remote 三态可切,同构契约 |
| 编排 | ReAct(LLM 工具循环)+ 出话评估/重写/润色流水线 | Runtime 确定性 observe/act 主循环 + 代码构造的 `skill_plan.v1` |
| 特色强项 | 议价阶梯引擎、consent+pending+确定性重放、Langfuse、多档鉴权+会话翻篇+冷快照、真实系统集成 | 规格+三级发布门禁、确定性安全评测、六道闸安全数据导入、统一返回信封 |
| 成熟度画像 | 业务能力丰富、已接真实系统 | 业务是合成数据,但**工程/测试/文档纪律接近生产项目** |

---

## 二、逐维度对比

### 1. 架构分层 / 编排
- **Skill-MCP**:四层**编译期物理隔离**(Runtime / Skill / MCP Tool / Knowledge),靠 `Protocol` 接口依赖;Skill 只能产出 `AgentAction`,拿不到 DB 句柄,要数据必须发 `CapabilityAction`,由 `CapabilityResolver` 把语义 capability 映射成物理工具名。Runtime 主循环确定性(`max_steps=8`,前/后置 Guardrail,超步强制转人工兜底)。
- **本项目**:多智能体画像 + 统一 EcomAgent ReAct 引擎;工具直接按名调用;编排靠 LLM 工具循环 + reply_pipeline。分层更多靠约定,不是物理隔离。
- **差距**:本项目缺一层"语义能力↔物理工具"的映射与"确定性 plan 契约",导致编排更依赖 LLM 自觉、可测试性弱。

### 2. 知识检索
- **Skill-MCP**:facet 拆卡(一个产品拆 specs/price/warranty/comparison 多张小卡)+ 倒排索引 **L0 类目/L1 概览/L2 叶子三层** + QueryPlan(规则优先,置信度<0.72 才 LLM 兜底)+ 确定性 rerank(facet_bonus / hotness 自学习 / exact_phrase)+ **可解释证据链**(返回命中正文片段 evidence + `match_reason` 人话 + query_plan + 每路候选数,整链可审计)+ **知识治理护栏**(`_requires_dynamic_private_source` 前置拦截"实时/私有"类问题,卡片正文内嵌"该转动态系统"边界)。
- **本项目**:FTS5 + 向量混合召回 + 统一召回层(profile/LTM/STM/KB 四源)+ query_understanding 门控/改写 + ApeRAG 外部 KB。召回质量强,但**返回偏"文档+分数",缺可解释证据(命中了哪句、为什么)与"静态知识不答动态/私有问题"的前置治理护栏**。

### 3. 记忆
- **Skill-MCP**:Event-Entity 双层——append-only 事件流(原始、可追溯)+ 物化实体(压缩画像);召回按 `0.55词法+0.20时效+0.15业务权重+0.10热度` 打分取 top6,自动控 prompt 预算。抽取是硬编码正则(扩展性差)。
- **本项目**:STM(会话摘要)+ LTM(事实列表 + FTS5 + 策展去重)+ 结构化 profile(base/标签/工单)+ 记忆工具即时写。功能更全、有策展,但**长期记忆偏"事实快照",缺"事件流可追溯 + 时间衰减 + 业务权重排序"这套模型**。

### 4. MCP / 工具契约
- **Skill-MCP**:mock/mysql/remote **配置驱动三态切换**,三者跑同一份 `CustomerServiceMCPPlugin`、返回**完全同构**;统一返回信封 `{status, data, display_summary, suggested_next_actions, error_code, permission, trace}`;`ToolSchema` 带治理元数据(`operation_kind` / `idempotency_mode` / `permission_level` / `ownership_argument`),side_effect 工具诚实标 `unsupported_demo`;`error_code` 直接驱动话术回退。
- **本项目**:MCP 常开 + `ctx_user_id`/`ctx_token` 跨进程身份透传(我们刚接 hmdp 时做的),工具返回是**各自约定的 dict**,无统一信封、无治理元数据、无 `suggested_next_actions`。

### 5. 身份 / 权限
- **Skill-MCP**:两层校验——agent 侧 `arguments.user_id == request.user_id`,数据层 `order.user_id == user_id` **在 SQL 结果上二次校验**;且**刻意不把请求体 user_id 当可信主体**,显式标 `authenticated:false / request_supplied_demo_context`,并把"生产必须换服务端认证主体"列进门禁。
- **本项目**:有真 HMAC token 鉴权 + `owned_order` 归属校验(比 Skill-MCP 的"演示身份"更真);hmdp 集成里也做了归属二次校验。**本项目这块实际更强**,但缺 Skill-MCP 那种"把未认证缺口显式标注 + 写进生产门禁"的纪律。

### 6. 安全数据导入(Skill-MCP 最大特色,本项目几乎空白)
- **Skill-MCP**:把"授权业务导出(CSV/JSON)→ 受控读模型"拆成**六道闸**:① 清单/路径安全(防目录穿越、单文件≤50MB、拒重复 key)② 声明式字段映射到白名单规范字段 ③ 类型转换+逐字段校验(错误信息**不落原始值**防 PII 泄漏)④ 强制脱敏(strict 默认,保留业务标识需 `--allow-operational-identifiers` 显式 opt-in;确定性 token 化)⑤ 跨数据集状态机勾稽(订单↔支付↔物流状态矩阵、金额勾稽、时间线)⑥ 校验和人工确认(默认 dry-run 不碰库,`--apply` 必须回填 dry-run 的 sha256,`hmac.compare_digest` 比对防 TOCTOU)+ 命名锁 + 单事务 UPSERT/回滚。
- **本项目**:`mock_data.py → seed.py → SQLite` 直灌,或直连 hmdp。**完全没有"外部真实数据 → 受控导入"的可审计通道**。这是本项目从"演示/接单库"走向"接任意真实店铺数据"最缺的一环。

### 7. 评测
- **Skill-MCP**:确定性夹具(ScriptedLLM + ScriptedGateway)驱动**真实 AgentRuntime**,零 LLM 裁判,门槛 `--fail-under 1.0`;数据集 JSON + StrictModel(`extra=forbid`)+ 维度计数硬校验(120 用例 35/20/15/15/15/10/10);**安全/权限/注入是一等评测维度**(7 维里 3 个):`must_not_call`(越权时根本不调工具)、分类 canary 检测系统提示泄露、连 `permission_denied` 错误码都列进 `forbidden_substrings`;"安全回归先写失败用例再修复";知识检索用 Recall@1/@3/MRR 门槛。
- **本项目**:有 eval dataset(cases.json)+ runner + regression(baseline/tolerance)+ sandbox + trace_to_case,能用真 LLM 判。但**缺"确定性夹具驱动真实栈"的可复现控制面评测**,也**没有把安全/权限/注入作为一等维度 + must_not_call + canary + 门槛=1.0**。

### 8. 规格 + 生产门禁(Skill-MCP 最可移植的纪律)
- **Skill-MCP**:六份规格(产品/系统/接口/数据/评测/非功能)+ 索引,统一**四态标记**「当前 / 演示 / 目标 / 范围外」;**三级发布门禁**(工程演示完成 → 真实数据试运行 → 生产候选),每级有可勾选验收清单;**每份规格结尾自曝"当前差距"**;非功能规格列了 15 项"接真实数据前的生产阻断清单"(认证主体/对象级授权/密码哈希升级/限流/服务间 TLS/DB 最小权限/密钥管理/输入校验/高风险写操作确认+幂等+人工复核/依赖扫描…)。
- **本项目**:docs 很多(分期文档、审查报告、集成方案),但**没有"四态标记 + 三级门禁 + 每文档自曝差距"这套把"已实现 vs 目标"钉死的纪律**,也没有统一的"接真实数据前生产阻断清单"。

### 9. 工程化基线
- **Skill-MCP**:PowerShell 质量门禁编排器(537 行,**状态机退出码 passed/failed/blocked(2)/skipped(3),blocked≠passed**)、JSON+MD 双份报告、**落盘前递归脱敏**、GitHub Actions 矩阵分作业 + 最终 quality-summary 聚合(缺 artifact 也生成完整聚合、缺项→blocked)、MySQL 证据**只认 CI 一次性服务 + 强制库名正则**(本地拒连库)、AGENTS.md 事实来源优先级 + Definition of Done("交付明确列出已验证/未验证/生产差距,不夸大")。
- **本项目**:有 launch.json / docker-compose / .superpowers SDD 台账 / 分期文档,但缺"报告状态机(blocked≠passed)+ 落盘脱敏 + CI 证据边界(防误连库)"这层。

### 10. 模型适配
- **Skill-MCP**:统一 Protocol(`complete(prompt,context)`);**装配层按密钥有无选实现**(有 key → DeepSeek,无 key → Echo 直接回显),视觉无 key → None 优雅降级;业务代码零 `if api_key` 分支。
- **本项目**:resilience factory(主备 fallback + 熔断 + 重试分类),比 Echo 回退更"生产",但**缺"无密钥也能全链路跑通(Echo/离线)"的开发/CI/评测友好模式**。

---

## 三、本项目已更强、无需借鉴(保持)

诚实起见,以下本项目明显领先,不要被 Skill-MCP 带偏:
- **议价确定性引擎**(`compute_offer`:阶梯让价、永不破底、可单测)——Skill-MCP 无议价。
- **风险动作确认流**(consent 门 + pending 挂起 + 服务端**确定性重放**,退款/成交/取消/改址),比 Skill-MCP 的"提示词让 LLM 先确认"更硬。
- **出话接地流水线**(evaluate/rewrite/polish,禁止编造、接地真实工具结果)。
- **真鉴权 + 会话生命周期**(HMAC token、多档用户、服务端签发会话+翻篇+冷快照兜底)——比 Skill-MCP 的"演示身份"真。
- **真实外部系统集成**(跨语言接 hmdp Java 系统,MCP 适配 + 身份/凭据透传)——Skill-MCP 只有合成数据。
- **可观测**(Langfuse 一轮一 trace 嵌套树 + 自研 tracer)——Skill-MCP 只有 trace_events。
- **并发/生产加固**(限流器/成本闸线程安全、生产 fail-closed、记忆并发原子性——本轮 review 已修)。

---

## 四、可借鉴优化清单(按 ROI 排序)

### P0 —— 最高性价比,直接补短板 / 提升可信度

**P0-1 · 安全数据导入框架(本项目最大空白,直接决定"能不能接真实店铺数据")**
- 借鉴:Skill-MCP 的六道闸(声明式映射→白名单校验(错误不落原值)→强制脱敏(operational 需 opt-in)→状态机勾稽→dry-run+checksum 人工确认→命名锁+单事务 UPSERT/回滚)。
- 落地:在本项目新增 `app/data_import/`,做一个 `import_business_export.py` 运维脚本 + `bundle/schema/privacy/state_matrix/importer` 五模块;先支持 CSV/JSON → 本项目 orders/products 读模型。可**整包参考** Skill-MCP 的 `backend/app/infrastructure/data_sources/`。
- 收益:一句"我有可审计的真实数据导入通道(脱敏+勾稽+校验和确认+事务)"在面试/生产评估里分量极重;成本中(1-2 天)。

**P0-2 · 评测升级:安全/权限/注入一等维度 + 确定性夹具驱动真实栈 + 门槛 1.0**
- 借鉴:`must_not_call`(越权时根本不调工具)、分类 canary 检测系统提示/内部字段泄露、把 `permission_denied` 等错误码也列入 `forbidden_substrings`、用脚本化 LLM/工具夹具跑**真实** orchestrator(可复现)、门槛=1.0、"安全回归先写失败用例再修复"。
- 落地:扩本项目 `app/evaluation/`——① dataset 增 `primary_dimension` 维度(intent/slot/权限/注入/降级/转人工),② 加 `ScriptedLLM`+`ScriptedGateway` 夹具驱动 orchestrator,③ 安全断言(forbidden_substrings/must_not_call/canary),④ regression 门槛调 1.0 且 blocked≠passed。
- 收益:把本项目已有的确认流/归属校验/护栏用**确定性回归**锁住,防回退;安全维度是客服 Agent 最该秀的。成本中。

**P0-3 · 规格四态标记 + 三级发布门禁 + 每文档自曝差距(便宜、提可信度)**
- 借鉴:「当前/演示/目标/范围外」四态词汇 + 「工程演示完成→真实数据试运行→生产候选」三级门禁清单 + 每文档结尾"当前差距" + 一份"接真实数据前生产阻断清单"(15 项)。
- 落地:在本项目 `docs/` 新增 `specs/README.md`(四态定义)+ `production-gates.md`(三级门禁 + 阻断清单),把现有能力逐条标 当前/目标。
- 收益:极低成本,极大提升"诚实工程"观感;和本项目已有的"审查报告/已知取舍"风格天然契合。

### P1 —— 结构性增强

**P1-1 · 确定性 plan 层 + 语义 capability↔物理工具两级映射**
- 借鉴:`skill_plan.v1`(intent→capability→required_slots→missing_slots→next_step,代码确定性构造,把 LLM 关进笼子)+ CapabilityResolver。
- 落地:在 orchestrator 里,LLM 意图分类后**用代码映射表**推导 capability/必填槽/缺槽/下一步,产出结构化 plan 存 turn 状态 + 发事件(前端/tracer 可视);工具调用经一层 capability→tool 名映射。
- 收益:编排可测试、可审计、缺槽追问确定化;和本项目 query_understanding 天然衔接。成本中。

**P1-2 · MCP 统一返回信封 + 治理元数据**
- 借鉴:`{status, data, display_summary, suggested_next_actions, error_code, permission, trace}` + `ToolSchema{operation_kind, idempotency_mode, permission_level, ownership_argument}`;`error_code` 驱动话术回退。
- 落地:给本项目 hmdp-mcp(及本地工具)统一返回信封,`suggested_next_actions`(如 `["retry","human_handoff"]`)让编排层无需 per-tool 特判即可降级/转人工。
- 收益:工具层可审计、可降级、可扩展;成本中低。

**P1-3 · 可解释检索证据链 + 知识治理前置护栏**
- 借鉴:检索返回 `evidence`(命中正文片段+分)+ `match_reason`(人话为什么命中)+ query_plan 审计;`_requires_dynamic_private_source` 前置拦截"实时库存/我的保修/验证码/包裹具体位置"等,不让静态知识乱答。
- 落地:本项目统一召回层返回结构里加 evidence/match_reason;query_understanding 前加"动态/私有问题"识别 → 直接走查单/转人工而非 KB。
- 收益:客服合规 + 人工兜底证据;成本低中。

**P1-4 · 身份"未认证显式标注"纪律 + 高风险写操作门禁清单**
- 借鉴:即便有真鉴权,也把"哪些接口/路径当前未做对象级授权/租户隔离"显式标注并列入门禁;高风险写操作强制"确认+幂等键+审计+人工复核,禁止自动执行支付/退款"。
- 落地:本项目已有确认流,补一份"高风险写操作清单 + 现状标注"进 production-gates.md;给退款/取消补幂等键(本项目 registry 幂等仅本地路径,MCP 路径要在 hmdp 侧保证——本轮已识别)。
- 收益:把已有安全能力"文档化成可验收门禁";成本低。

### P2 —— 锦上添花

- **P2-1 · Event-Entity 记忆升级**:LTM 从"事实列表"升级为"append-only 事件流 + 物化实体 + 多路打分召回(词法/时效/业务权重/热度)",获得可追溯 + 时间衰减 + prompt 预算控制。可保留本项目现有 FTS/策展,叠加事件流层。成本中。
- **P2-2 · facet 拆卡 + hotness 自学习**:知识卡按 facet 拆(问价格命中 price 卡而非 specs 卡),检索命中次数回写做自适应热度加权。成本低。
- **P2-3 · 工程化:报告状态机(blocked≠passed)+ 落盘前脱敏 + CI 证据边界(防误连库)+ Echo/离线无密钥模式**。成本低中,直接抬工程观感。

---

## 五、一句话行动建议

按 **P0-3(半天,先立门禁与四态纪律)→ P0-1(安全数据导入框架,补最大空白)→ P0-2(评测安全维度+确定性夹具)** 的顺序做,三项做完,本项目就从"能力丰富的演示"升级为"有生产纪律、可接真实数据、安全可回归"的样本——这正是 Skill-MCP 唯一比本项目强、且最值得搬的地方。P1/P2 视时间叠加。
