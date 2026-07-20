# 借鉴 nanobot 的可改进点分析

> 分析对象:[HKUDS/nanobot](https://github.com/HKUDS/nanobot)——开源个人 AI Agent 运行时(Python,~6.9 万行,MIT)。
> 目的:找出可迁移到本电商客服 Agent 项目的**生产级工程做法**。
> 结论先行:nanobot 是通用个人助手(多聊天渠道 / shell 工具 / 自动化),体量远大于我们;但它在**模型容错、Agent 循环架构、上下文安全裁剪、记忆更新范式、安全防护**上有几处成熟做法,直接能补强我们的"可上线"底座。

---

## 一、快速结论:Top 6 可借鉴项(按对本项目的 ROI 排序)

| # | 借鉴点 | 价值 | 工作量 | 落到我们哪个模块 |
|---|--------|------|--------|-----------------|
| 1 | **模型 fallback + 熔断 + 错误分类重试** | ⭐⭐⭐ 高 | 中 | 新增 `app/resilience/` 或并入 W3.5 加固 |
| 2 | **Agent 生命周期 Hook 体系(可插拔)** | ⭐⭐⭐ 高 | 中高 | 重构 `app/api/streaming.py` + 护栏/HITL/可观测 |
| 3 | **历史裁剪的"协议合法性"保证** | ⭐⭐ 中高 | 低 | `app/agent/chat.py` 的 `_compress_history` / `_build_messages` |
| 4 | **Skill 声明式依赖检测 + 渐进加载** | ⭐⭐ 中 | 低 | `app/agent/skills/loader.py` |
| 5 | **配置 `${ENV}` 占位 + schema 迁移 + 缺省补齐** | ⭐⭐ 中 | 低 | `app/config/settings.py` |
| 6 | **Dream 式异步反思记忆(用户画像)** | ⭐⭐ 中 | 中高 | `app/agent/memory/` + 一个 cron/脚本 |

> 明确**不建议照搬**的:30+ provider 注册表(过度工程,我们模型就一两家)、bwrap 系统级沙箱(我们没有开放 shell/任意代码执行工具)、多聊天渠道抽象(单一 Web 客服场景暂不需要,未来接企微/公众号再说)、SSRF 防护(**当前我们工具只查本地 SQLite,无外部 URL 调用,暂不需要**;一旦新增"调用外部订单系统/webhook"类工具,再补,见附录)。

---

## 二、逐项详解

### 1. 模型 fallback + 熔断 + 错误分类重试 ⭐⭐⭐

**nanobot 怎么做**(`providers/fallback_provider.py`、`providers/base.py:861-974`):
- **错误分类**:把错误分为"瞬时"(429/500/502/503/504/timeout)与"永久"(余额不足、400/401/403/404/422),结构化元数据优先、正则文本兜底;只对瞬时错误重试。
- **多级 fallback**:`FallbackProvider` 包住主模型,主模型失败且"换模型有用"时,按配置顺序尝试备用模型(可跨厂商)。
- **熔断器**:主模型连续失败 3 次→跳闸 60 秒,期间直接走备用,冷却后放一个探测请求(half-open)。
- **流式友好**:已经吐出内容后再报错,不整体重试(避免重复/断裂),仅 timeout 允许开新段续跑。
- 还有个高频坑修复:`_enforce_role_alternation` 合并连续同角色消息、去掉尾部裸 assistant、修复 system→assistant 边界——**接国产模型(通义/Kimi 等)网关经常因 role 顺序/空 content 报 400**。

**我们现状**:`chat.py` / `orchestrator.py` 直接用单个 `OpenAI` client,**没有任何 fallback / 重试 / 熔断**。一旦主模型 429 或抽风,整个请求就失败。

**怎么用到我们项目**:
- 用我们已经验证过的**透明代理模式**(和 W2 的 `TracingClient` 一模一样的手法)包一层 `ResilientClient`:拦截 `chat.completions.create`,内部做"错误分类重试 + 主备切换 + 熔断"。**核心零改动**,在服务层注入,和 TracingClient 可叠加。
- `.env` 增配 `FALLBACK_MODEL` / `FALLBACK_BASE_URL`;熔断状态上报到 W2 看板(多一张"主模型熔断次数/fallback 命中率"卡片)。
- 不必抄 30+ provider 注册表,只做"主 + 1 个备"两级即可。

**面试点**:"我用透明代理给 LLM 调用加了错误分类重试 + 主备熔断,主模型 429/超时时自动切备用,可用性从单点变成双活。"

---

### 2. Agent 生命周期 Hook 体系 ⭐⭐⭐

**nanobot 怎么做**(`agent/hook.py`、`agent/loop.py`):
- 主循环是**显式状态机**(`RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE`),每个状态记录耗时/错误到 `StateTraceEntry`。
- 一整套**生命周期钩子**:`before_run / before_iteration / before_execute_tool / after_execute_tool / on_execute_tool_error / finalize_content …`;`CompositeHook` 扇出并**对每个子 hook 异常隔离**(单个坏 hook 不打崩主循环)。
- 具体能力(如"文件编辑活动→WebUI 进度事件")就是一个独立 hook,而不是塞进主循环 if-else。

**我们现状**:护栏(输入短路/输出脱敏)、HITL(升级判定)、可观测性(埋点)目前是在 `run_agent_streaming` 里**命令式硬编码**串起来的(限流→人工→快路径→成本→护栏→Agent→护栏→升级)。功能没问题,但耦合度偏高、加新治理逻辑要改主流程。

**怎么用到我们项目**:
- 把 `run_agent_streaming` 的治理链重构成一个**Hook 管道**:`before_request`(限流/护栏输入/快路径/成本)、`after_reply`(护栏输出/升级判定)、`on_error`。每个 hook 独立、可单测、可开关、异常隔离。
- 好处:护栏/HITL/加固从"散在主流程"变成"注册的 hook",新增治理逻辑不动主循环——这正是"不动核心"原则的进一步贯彻。
- **注意**:这是**架构优化**,当前功能已能用;建议作为一次专门的重构(有 120+ 测试兜底,重构风险可控)。

**面试点**:"我把安全护栏、人机协作、可观测性重构成 Agent 生命周期 Hook,主循环只负责编排,治理能力全部可插拔、异常隔离。"

---

### 3. 历史裁剪的"协议合法性"保证 ⭐⭐

**nanobot 怎么做**(`session/manager.py.get_history`):多轮长对话按 token 预算裁剪时,严格保证:① 不从工具调用中间截断;② 丢弃"悬空的 tool result"(前面的 assistant tool_calls 被裁掉了);③ 找一个"合法起点"回放。否则模型侧会因 `tool_calls` 与 `tool` 消息不成对而报 400。

**我们现状**:`chat.py._compress_history` 已有部分处理(`while split>0 and raw_messages[split].role in ("tool",): split-=1`),但只处理了"起点是 tool 消息"一种情况,**没处理"assistant 有 tool_calls 但对应 tool 消息被裁掉"**这种更隐蔽的非法状态。长对话 + 工具调用密集时可能触发模型 400。

**怎么用到我们项目**:
- 把 nanobot 的"找合法起点 + 丢弃悬空 tool result"算法抽成一个工具函数(如 `app/agent/history_utils.py::legal_message_start`),在 `_compress_history` 和 `_build_messages` 里用。
- 低工作量、高稳健性收益,尤其我们工具调用不少(查单/物流/退款/RAG)。

---

### 4. Skill 声明式依赖检测 + 渐进加载 ⭐⭐

**nanobot 怎么做**(`agent/skills.py`、`skills/*/SKILL.md`):
- SKILL.md frontmatter 支持 `requires.bins` / `requires.env` **声明依赖**;启动时检测 CLI/环境变量是否满足,不满足的技能从可用列表过滤掉,但仍在 summary 里显示"unavailable + 原因"。
- **三层渐进加载**:元数据(常驻,~100词)→ SKILL.md 正文(触发才加载)→ 绑定资源(按需 read_file);全量技能列表不占满 context。
- 工作区技能覆盖内置同名技能。

**我们现状**:`SkillManager` 已做渐进加载(catalog 摘要 + `load_skill` 按需)——这块我们已经不错。**缺的是"声明式依赖检测"**:某技能依赖内部系统 key 时,现在只能运行时报错。

**怎么用到我们项目**:
- 给 `SKILL.md` frontmatter 加 `requires.env`(如某退款技能需要 `REFUND_API_KEY`),`loader.py` 启动时检测,缺配置的技能标灰 + 原因,运营一眼看到"哪些技能因缺配置不可用"。小改动、体验提升明显。

---

### 5. 配置系统:`${ENV}` 占位 + schema 迁移 + 缺省补齐 ⭐⭐

**nanobot 怎么做**(`config/loader.py`):
- 配置里可写 `${ENV_VAR}` 占位符,加载时递归替换。
- **多版本配置迁移** `_migrate_config`:旧字段自动迁移到新结构并打 warning。
- `merge_missing_defaults`:升级后旧配置缺的字段自动补默认值,不覆盖用户已配。
- **签名比对式惰性热更新**:不 watch 文件,每次取 runtime 时比对"影响行为的字段"是否变化,变了才重建对象——轻量、无监听器复杂度、配置变更不重启。

**我们现状**:用 `pydantic-settings` 从 `.env` 读,够用但没有迁移/占位/热更新。

**怎么用到我们项目**:
- 优先级最高的是**签名比对式热更新**:比如线上想调 `hitl_confidence_threshold`、`rate_limit_per_min`、切换主/备模型,不用重启服务——运营改配置即时生效。实现成本低(一个"配置签名"函数 + 惰性比对)。
- 占位符/迁移对我们当前简单配置收益一般,可选。

---

### 6. Dream 式异步反思记忆 ⭐⭐

**nanobot 怎么做**(`agent/memory.py` 的 Dream):
- 长期记忆是人类可读的 markdown(`USER.md` 用户画像 / `MEMORY.md` 长期事实),由 append-only 的 `history.jsonl` 驱动。
- **一个独立 cron 任务**(不走主循环)定期取"上次游标之后的新事件",用受限工具(只读写文件)跑一次 ephemeral agent,让模型**回头看整段对话**去编辑画像文件。
- **关键设计**:游标推进以"文件真实 diff"为准,而非 LLM 自称"改完了"——防幻觉自证、防数据丢失。

**我们现状**:`memory/long_term.py` 是**同步、逐轮**做事实抽取/摘要,容易漂移、且占在线延迟/成本。

**怎么用到我们项目**:
- 把"客户画像 / 常见问题类型"的长期记忆更新,改成**离线异步 cron 反思任务**(读会话历史 → 回头归纳 → 更新画像),在线只读不写。既省在线成本、又因"整体回看"归纳质量更高、漂移更小。
- **顺带一个通用原则可复用到我们的评估回归**:判断一次批处理是否真生效,要看"真实产出/diff",不要信模型自称"完成"——这和我们 `reflow`/`run_eval` 的严谨性一脉相承。

---

## 三、建议的落地顺序

1. **模型 fallback + 熔断**(#1)——生产可用性直接相关,复用现成的透明代理模式,性价比最高,建议最先做。
2. **历史裁剪合法性**(#3)+ **Skill 依赖检测**(#4)+ **配置热更新**(#5)——都是低工作量、独立、低风险的稳健性/体验增强,可穿插做。
3. **Hook 体系重构**(#2)——架构级改进,收益大但要动主流程,放在有整块时间时做,靠 120+ 测试兜底。
4. **Dream 异步记忆**(#6)——锦上添花,想深化"记忆"这条线时再做。

---

## 附录:SSRF 防护(条件性,当前暂不需要)

nanobot `security/network.py` 有一套很硬的**应用层 SSRF 防护**:DNS 解析后校验真实 IP(防 DNS rebinding)、pin 住 DNS 结果(防 TOCTOU)、重定向后二次校验、私网/云元数据网段黑名单。

**我们当前不需要**:客服工具只查本地 SQLite + 本地 RAG,没有"用用户输入去访问任意 URL"的能力,没有 SSRF 面。

**何时补**:一旦新增"调用外部订单/物流真实 API""webhook 回调""让 Agent 抓取用户给的链接"这类工具,**必须**补这套(仅做 URL 字符串黑名单是不够的)。到时直接借鉴 `security/network.py` 的四步:解析 IP→校验→pin DNS→重定向后再校验。

---

> 分析方法:通过只读代码走查 nanobot 的 providers / agent / memory / security / session / channels / skills / config 模块得出;代码行号引用见对应源文件。本项目与 nanobot 均为 MIT,借鉴其**做法/思路**即可,注意保留必要出处。
