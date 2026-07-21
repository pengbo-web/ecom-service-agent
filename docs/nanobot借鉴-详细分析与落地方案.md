# 借鉴 nanobot —— 详细代码级分析与落地方案

> 分析对象：[HKUDS/nanobot](https://github.com/HKUDS/nanobot) —— 开源通用个人 AI Agent 运行时（Python，约 11 万行，MIT）。
> 本文是对已有《借鉴nanobot-可改进点.md》的**代码级细读升级版**：所有结论都带 `文件:行号` 依据，并给出对本项目（ecom-service-agent，分支 `feature/w1-service-streaming`）的落地方案。
> 分析方法：对 nanobot 的四个子系统（Agent 循环与上下文管理、Hook 体系、记忆/技能/子代理、Provider 与容错）分别做了逐文件精读。
> nanobot 路径前缀统一为 `nanobot/`；本项目路径前缀统一为 `app/`。

---

## 0. 总原则

nanobot 是**通用、自治、多渠道**的个人助手（多聊天渠道 / shell 工具 / 后台自动化 / 子代理委派），体量与目标都远超我们这个**单场景、同步、Web 客服** bot。因此：

- **只借"能补我们上线短板"的工程做法**，不照搬为通用/自治场景服务的重型架构。
- 判据：这项东西是否解决我们**真实存在**的问题？对一个小客服 bot 是否**过度工程**？

一句话结论：nanobot 在 **模型容错、上下文防线、风险动作前置授权** 三块的做法直接能补强我们的"可上线"底座；记忆策展、鲁棒性阶梯、若干小改进值得挑着做；其余多数不必碰。

---

## 一、可借鉴项总览（按 ROI 排序）

| # | 借鉴点 | 价值 | 工作量 | 落到本项目哪个模块 | 相对旧文档 |
|---|--------|------|--------|-------------------|-----------|
| 1 | 模型 fallback + 熔断 + 错误分类重试 | ⭐⭐⭐ | 中 | 新增 `app/resilience/llm_client.py`（透明代理，可叠加 TracingClient） | 已提，本文给出可落地方案 |
| 2 | 上下文防线三件套（结果截断 / token 预算触发 / 裁剪协议合法性） | ⭐⭐⭐ | 低-中 | `app/agent/chat.py` + 新增 `app/agent/history_utils.py` | **本次新增/收紧** |
| 3 | 风险动作「前置授权门」（默认拒绝的 ContextVar 消费门） | ⭐⭐⭐ | 低 | `app/agent/tools/`（refund / negotiate_price）+ 服务层 | **本次新增** |
| 4 | 治理链 Hook 化（观察 vs 授权分离） | ⭐⭐ | 中-高 | 重构 `app/api/streaming.py` | 已提，本文收紧为"分离"洞见 |
| 5 | 记忆：离线反思 + 策展式 prompt | ⭐⭐ | 中 | `app/agent/memory/` + 一个 cron/脚本 | 已提，本文补策展 prompt 细节 |
| 6 | 鲁棒性阶梯（空回复重试 / 畸形工具调用降级） | ⭐⭐ | 低 | `app/agent/chat.py` 的 `_react_loop` | **本次新增** |
| 7 | Skill：`yaml.safe_load` + 声明式依赖检测 | ⭐ | 低 | `app/agent/skills/loader.py` | 已提，本文补解析健壮性 |
| 8 | 配置签名式热更新（免重启调阈值） | ⭐ | 低 | `app/config/` | 已提 |

---

## 二、第一梯队（强烈建议）

### 1. 模型 fallback + 熔断 + 错误分类重试 ⭐⭐⭐

**nanobot 怎么做**

- **错误即数据，不抛异常**：`LLMResponse`（`nanobot/providers/base.py:149`）除 `content/tool_calls/finish_reason/usage` 外，携带**结构化错误信封** `error_status_code / error_kind / error_type / error_code / error_retry_after_s / error_should_retry`（`base.py:159-165`）。所有 provider 把异常映射成 `finish_reason="error"` 的响应——这是"重试/切换"能跨 provider 统一的根基。
- **错误分类** `_is_transient_response`（`base.py:401-496`）：优先看 `error_should_retry`，再看状态码，再看 `error_kind`，最后正则兜底文本。关键细节：**429 被拆成两类**——欠费/配额（不可重试）vs 速率限制（可重试）（`base.py:220-237`），未知 429 默认等待重试。`Retry-After` 从响应字段、结构化 `error_retry_after_s`、响应头（含 `retry-after-ms`/HTTP-date）、乃至错误文本正则里逐级解析（`base.py:766-839`）。
- **重试循环** `_run_with_retry`（`base.py:861`）：退避 `(1,2,4)` 秒（`base.py:198`）；"persistent"模式无限重试但单次上限 60s、连续 10 次相同错误即放弃。
- **多级 fallback** `FallbackProvider`（`nanobot/providers/fallback_provider.py:59`）：`_should_fallback`（`:292`）—— **永不切换**：`{400,401,403,404,422}` 或 auth/permission/content_filter/refusal/context_length/invalid_request（`:24-32`）；**切换**：`{408,409,429}`/5xx、timeout/connection/server_error/rate_limit/overloaded、以及欠费/配额（`:294-316`）。
- **熔断器**（`fallback_provider.py:108-115,193-199`）：主模型连续 3 次可切换失败 → 跳闸，冷却 60s 内直接走备用，之后半开放一个探测请求。状态挂在实例上，跨轮次存活。
- **流式友好**（`:166-178`）：已吐出内容后报错默认**不**整体重试（避免重复/断裂）；仅 `timeout` 且提供 `on_stream_recover` 时，关掉当前段、开新段续跑。
- **客户端配置**：OpenAI-compat client 设 `max_retries=0`（SDK 重试关掉、nanobot 自己管），请求超时 120s（`nanobot/providers/openai_compat_provider.py:89,495-501`）。fallback 名单来自 `config.agents.defaults.fallback_models`（`factory.py:165`）。

**本项目现状**

在约 8 处直接构造并调用**单个** `openai.OpenAI(base_url=Qwen)`：`app/agent/chat.py:19`、`app/multi_agent/orchestrator.py`、`agents.py`、`router.py`、`app/agent/summarizer.py`、`app/agent/memory/extraction.py`、`app/agent/rag/embedder.py:23`、`app/agent/tools/knowledge.py`。**无 fallback / 无重试 / 无熔断**。主模型 429 或抽风 → 整条请求失败。服务层虽有 `cost_guard`/`rate_limiter`，但那是限流不是容错。

**落地方案（~80 行，不引入框架）**

1. 新增单一入口 `app/resilience/llm_client.py`，暴露 `resilient_chat_completion(**kwargs)`，让上述 ~8 处都改走它（唯一收敛点）。做法同 W2 的 `TracingClient` **透明代理**，可叠加。
2. 内部持两个 client：`primary`（Qwen）、`secondary`（备用模型/端点，`.env` 增 `FALLBACK_MODEL`/`FALLBACK_BASE_URL`），均 `max_retries=0` + 显式 `timeout`。
3. 逻辑：调 primary → 异常用**移植版** `_classify(exc)` 分瞬时/永久 → 瞬时按 `(1,2,4)` 退避并遵守 `Retry-After` → 耗尽或遇可切换错误 → 切 secondary（各自 model id）→ 首个成功即返回。
4. 加轻量**熔断**（3 连败 → 60s 冷却，约 15 行，抄 `fallback_provider.py:108-115`）。熔断次数/命中率上报 W2 看板多一张卡。
5. **保持同步**（匹配本项目），不需要流式续跑那套；结构化输出/摘要/记忆抽取一并受益。

**移植时重点挖的源码**：`base.py:401-496`（分类）、`base.py:766-839`（Retry-After 解析）、`base.py:861-974`（重试骨架）、`fallback_provider.py:108-115`（熔断）、`fallback_provider.py:292-316`（切换判定）。

> **不要**照抄 40+ provider 注册表（`registry.py`）：我们模型就一两家，两级 `[primary, secondary]` 是对的高度。

**面试点**："我用透明代理给 LLM 调用加了错误分类重试 + 主备熔断，主模型 429/超时自动切备用，可用性从单点变双活。"

---

### 2. 上下文防线三件套 ⭐⭐⭐

**本项目现状（问题）**：`app/agent/chat.py` 的 `_compress_history` 在 `raw_messages` 超过 10 条时把最老的摘要成一段 summary（保留最近 3 条）。这套 **既过度压缩**普通短对话，**又防不住真正威胁**：① 一次大 tool 结果（整单/长检索）就能撑爆窗口；② `_compress_history` 只处理了"起点是 tool 消息"一种情况，没处理"assistant 带 tool_calls 但对应 tool 消息被裁掉"这种更隐蔽的非法态，长对话 + 工具密集时会触发模型侧 400。

nanobot 的关键设计是**"持久化历史"与"送模型的副本"分离**：所有修补/压缩都作用在每轮的一个**丢弃副本**上，存档的 transcript 始终干净（`context_governance.py` 文档串、`runner.py:355-364`）。

**2a. tool 结果截断 / 超限落盘** — `normalize_tool_result`（`nanobot/agent/context_governance.py:109-136`）
超大工具输出写盘，历史里只留一个"可再 `read_file`"的指针；`read_file` 自身豁免以防 persist→read→persist 环。
> 落地：在 `app/agent/tools/registry.py::execute_tool` 或 `chat.py` 追加 tool 结果前，统一把结果截到 N 字符（可选：超大写入 `app/sessions/` 留指针）。**这是最高性价比的一条**——你的查单/RAG 结果都可能很大。

**2b. 按 token 预算触发压缩（替代"超 10 条"）** — `context_governance.py:92-107`
`input_budget = context_window − max_output_tokens − 1024 安全缓冲`；`Consolidator.maybe_consolidate_by_tokens`（`nanobot/agent/memory.py:984`）在预算超限时才压缩。
> 落地：把 `len(raw_messages) > 10` 换成 `estimate_tokens(messages) > 窗口 − 输出 − 缓冲`。10 条规则对短对话过压、对单条大结果失灵；token 预算精准命中真实上限。

**2c. 裁剪的"协议合法性"** — `context_governance.py:176-296`、`memory.py:771`
压缩只在 **user 回合边界** 切（`pick_consolidation_boundary`），绝不拆散 `tool_call`/`tool_result` 对；发送前的自愈流水线：剥离畸形 tool_calls（`:176`）、丢弃悬空 tool 结果（`:231`）、回填缺失 tool 结果（`:257`）；`snip_history`（`:380`）从最新往回填预算并把尾部锚定到合法 user 起点（`find_legal_message_start :437-446`）。
> 落地：抽一个 `app/agent/history_utils.py::legal_message_start()` + `sanitize_tool_pairs()`，在 `_compress_history` 和 `_build_messages`（送 `chat.completions.create` 前）各调一次。低成本、高稳健，尤其我们工具调用密集（查单/物流/退款/RAG）。

---

### 3. 风险动作的「前置授权门」 ⭐⭐⭐（本次最贴合我们的新洞见）

**nanobot 怎么做** — `nanobot/agent/goal_permission.py`（约 30 行）
一个**默认拒绝**的 ContextVar 消费门：`_GOAL_MUTATION_ALLOWED` 默认 `False`（`:8`）；`goal_mutation_allowed()` 读、`@contextmanager goal_mutation_permission(allowed)` 在作用域内授权、退出即复位（`:22`）。授权由 `/goal` 命令显式压入本轮 scope（`nanobot/command/builtin.py:866`），**强制点在工具内部**：`long_task.py:202` 的 `create_goal` 检查未授权就硬拒绝，用后自撤销。特征：**每轮、能力粒度、默认拒绝、在动作边界强制**。

**本项目现状**：HITL 是**回复之后**才看 `confidence`/`requires_human` 判断要不要转人工（`app/api/streaming.py:37-46`）。而 `negotiate_price`（刚落地）、`apply_refund` 这类**副作用工具在执行前没有任何消费门**——模型自己决定就调了。

**落地方案**：仿 `goal_permission` 做一个 `app/agent/consent.py`（ContextVar，默认拒绝）。副作用工具（退款、超过某额度的让价、订单状态变更）在执行前检查消费门；未授权则返回"需用户确认"而非直接执行。授权来源：用户在对话中显式确认、或坐席在 HITL 面板放行。**把"观察"（事后判断转人工）与"授权"（事前放行动作）分开**——这正是下一条的架构洞见。

**价值**：从"先斩后奏 + 事后补救"变成"高风险动作事前门控",对退款/议价这类涉钱操作尤为关键。

---

## 三、第二梯队（值得做）

### 4. 治理链 Hook 化：真正该学的是"观察 vs 授权分离" ⭐⭐

**nanobot 怎么做** — `nanobot/agent/hook.py`
`AgentHook` 基类（`:63`）提供一组生命周期方法：`before_run/after_run/on_error`、`before_iteration/after_iteration`、`before_execute_tool/after_execute_tool/on_execute_tool_error`、以及唯一的变换型钩子 `finalize_content(ctx, text)->str`（`:139`）。`CompositeHook`（`:146`）扇出并**对每个子 hook 异常隔离**（`reraise` 可选）；`finalize_content` 是真正的**管道**（前一个输出喂后一个，`:250`）。

**关键判断（子代理审查偏保守，如实记录）**：对一个**单循环小 bot**，照搬整套 `AgentHook`/`CompositeHook`/工厂/turn-spec 属**过度工程**——nanobot 需要它是因为要复用 CLI/WebUI/cron/subagent 多入口。我们只有一个入口。

**真正的洞见**：nanobot 把 **观察**（hooks，**明确不能否决**动作）与 **授权**（第 3 条的 ContextVar 门，在工具边界强制）**分开**；而我们目前在 `app/api/streaming.py` 里把二者揉在一条命令式链里（限流→人工→快路径→成本→护栏→Agent→护栏→升级）。

**落地建议（轻量版）**：
- 借 `finalize_content` 的思路：把输出护栏做成一个 `finalize(text)->text` 变换回调（而非多 hook 管道）。
- 借 2-3 个工具生命周期回调（`before/after_execute_tool`）替换 `chat.py` `_react_loop` 里内联的 `_emit`，得到干净的埋点缝。
- **暂不**引入完整 hook 类体系。等真要接第三方插件/多渠道时再说。

### 5. 记忆：离线反思 + 策展式 prompt ⭐⭐

**nanobot 怎么做** — `nanobot/agent/memory.py`
- 存储 4 文件（`:66-84`）：`SOUL.md`（行为规则）、`USER.md`（用户画像）、`memory/MEMORY.md`（项目/长期事实）、`memory/history.jsonl`（append-only、**唯一事实源**，原子写 + fsync + 游标锁 + 容错解析）。三个 md 是从 history 派生的**策展视图**。
- **两个机制分离（核心洞见）**：`Consolidator`（`:741`，**上下文压缩**，token 触发，在 user 边界摘要老 span 进 history，失败则 raw dump 兜底）vs `Dream`（`build_dream_prompt :532`，**离线策展**，`/dream` 触发）。Dream 从**独立游标**读新事件、把 SOUL/USER/MEMORY 当前内容嵌进 prompt、用**受限文件编辑工具**（`build_dream_tools :593-633`）直接改画像文件。
- **策展纪律**：SNIP 标记 `[permanent]/[durable]/[ephemeral]/[correction]/[skip]`（模板 `consolidator_archive.md`）；MECE 跨文件去重、就地纠正、按龄衰减（sprint 目标 30 天归档等，`dream.md:78-83`）；**commit message 以真实 git diff 为准、不信 LLM 自称**（`memory.py:686-702`）——防幻觉自证。

**本项目现状**：`app/agent/memory/long_term.py` 是**同步逐轮**：`add_facts` = 追加 + 精确小写去重 + FIFO 截 50（`:85-94`）。问题：近义重复留存、老而重要的事实被新琐事挤掉；且占在线延迟/成本。

**落地方案（借 prompt，不借基建）**：
- 把会话结束的记忆更新从"追加去重截断"改成一次 **LLM 策展**：合并 / 就地纠正 / 按龄衰减 / 删或留，用 `consolidator_archive.md` + `dream.md` 的 SNIP 与规则设计 prompt。
- 可选：把单一 LTM JSON 按 MECE 拆成"用户偏好 / 行为规则"两段，注入更干净。
- 进一步：把长期画像更新改成**离线 cron 反思**（整体回看→归纳→只在离线写），在线只读——省成本、质量更高、漂移更小。
- **跳过**：git 版本化、dream-log/restore、append-only history 运行时、Dream 文件编辑引擎——在我们"每用户封顶 50 条"的规模下属过度工程。

> 通用原则可复用到我们的评估回归：判断一次批处理是否真生效，看**真实产出/diff**，不信模型自称"完成"——与 `reflow`/`run_eval` 的严谨一脉相承。

### 6. 鲁棒性阶梯（挑便宜的两条）⭐⭐

nanobot `runner.py` 有多级兜底：空回复重试（`:494`）、`length` 续写（`:526`）、畸形工具调用先重试再"不带工具"降级（`:821-848`）、预算耗尽的收尾 pass（`:920`）、LLM 墙钟超时（`:698-708`）。
> 落地：只移植两条最便宜的到 `chat.py._react_loop`——① 空 `content` 重试一次；② 工具调用畸形（无法 `json.loads` 参数/无名）时退回一次"不带 tools"的请求。直接减少用户可见的"⚠️ 出错了"。

---

## 四、第三梯队（顺手的小改进）

### 7. Skill：解析健壮性 + 声明式依赖 ⭐
- **`yaml.safe_load`**（nanobot `skills.py:250`）替换本项目 `app/agent/skills/loader.py:35-58` 的**手写正则** frontmatter 解析（当前只认扁平 `key: value`，遇嵌套/多行会崩）。低成本健壮性提升。
- 可选：SKILL.md frontmatter 加 `requires.env`（nanobot `skills.py:144-214` 的思路），启动时检测缺失的内部 key，把不可用技能标灰 + 原因，运营一眼看到。
- 注：本项目 Skill 的渐进披露（catalog + `load_skill` 工具）已做得不错，甚至对弱模型比 nanobot"直接 read_file"更清晰，**无需改**。

### 8. 配置签名式热更新 ⭐
nanobot `config/loader.py` 不 watch 文件，每次取 runtime 时**比对影响行为的字段签名**，变了才重建对象。
> 落地：给 `app/config/` 加一个"配置签名"函数 + 惰性比对，让线上调 `hitl_confidence_threshold`/`rate_limit_per_min`/切主备模型**免重启**生效。占位符 `${ENV}`/多版本迁移对我们当前简单配置收益一般，可选。

---

## 五、明确不建议借鉴

| 项 | 原因 |
|---|------|
| 40+ provider 注册表（`registry.py`） | 我们模型就一两家，两级主备足够 |
| bwrap 系统级沙箱 | 我们没有开放 shell / 任意代码执行工具 |
| 多聊天渠道抽象 | 单一 Web 客服；未来接企微/公众号再说 |
| subagent 异步后台委派（`agent/subagent.py`） | 我们是**同步分诊**（一 intent 一专家），router+orchestrator 已是对的形态；异步委派只会增延迟 |
| 显式状态机 FSM（`loop.py` 的 RESTORE→…→DONE） | 对线性 `chat()` 过度工程 |
| mid-turn 注入队列 / 断点续跑（`runner.py:401`、`loop.py:1860`） | 请求-响应式客服无需 |
| 完整 Hook 类体系 | 单循环 bot 过度工程，见第 4 条 |
| SSRF 防护 | 当前工具只查本地 SQLite、无外部 URL；**一旦新增"调外部订单系统/webhook"类工具再补** |

---

## 六、建议落地路线

1. **模型 fallback（第 1 条）** —— 最独立、最高可用性收益，透明代理手法与现成 `TracingClient` 同款，可叠加。
2. **上下文防线三件套（第 2 条）** —— 护 function-calling 循环不被大结果 / 悬空 tool_call 打崩。
3. **风险动作前置授权门（第 3 条）** —— 给 `negotiate_price` / `apply_refund` 加执行前确认，涉钱操作事前门控。
4. 之后按需：鲁棒性阶梯（6）→ 记忆策展（5）→ Hook 化重构（4，有 120+ 测试兜底）→ 小改进（7、8）。

每一项建议走本项目既有流程：`brainstorming → writing-plans → subagent-driven-development`，TDD + 逐任务规格/质量双审 + 端到端冒烟。

---

## 附录：关键源码索引（nanobot）

| 主题 | 文件:行 |
|------|--------|
| LLM 响应错误信封 | `providers/base.py:149,159-165` |
| 瞬时/永久错误分类 | `providers/base.py:401-496`（429 拆分 `:220-237`） |
| Retry-After 解析 | `providers/base.py:766-839` |
| 重试循环骨架 | `providers/base.py:861-974`（退避 `:198`） |
| fallback 切换判定 | `providers/fallback_provider.py:292-316`（不切名单 `:24-32`） |
| 熔断器 | `providers/fallback_provider.py:108-115,193-199` |
| 流式失败处理 | `providers/fallback_provider.py:166-178` |
| token 预算 | `agent/context_governance.py:92-107` |
| tool 结果截断/落盘 | `agent/context_governance.py:109-136` |
| 历史自愈（剥离/丢弃/回填） | `agent/context_governance.py:176-296` |
| 合法起点 / snip | `agent/context_governance.py:380,437-446` |
| 压缩边界 | `agent/memory.py:771,984` |
| Dream 离线策展 | `agent/memory.py:532,593-633,686-702` |
| ContextVar 授权门 | `agent/goal_permission.py`；强制点 `agent/tools/long_task.py:202` |
| Hook 基类/组合 | `agent/hook.py:63,146,250` |
| 鲁棒性阶梯 | `agent/runner.py:494,526,821-848,920` |
| Skill 解析/依赖 | `agent/skills.py:144-214,250` |

## 附录：本项目对应改动点索引

| 借鉴项 | 落到本项目 |
|--------|-----------|
| 模型 fallback | 新增 `app/resilience/llm_client.py`；收敛 ~8 处 `OpenAI(...)` 调用 |
| 上下文防线 | `app/agent/chat.py`（`_compress_history`/`_build_messages`/`_react_loop`）+ 新增 `app/agent/history_utils.py` |
| 前置授权门 | 新增 `app/agent/consent.py`；`app/agent/tools/refund.py`、`bargain.py` 加门控 |
| 治理链分离 | `app/api/streaming.py` |
| 记忆策展 | `app/agent/memory/long_term.py`、`extraction.py` + 一个离线脚本 |
| 鲁棒性阶梯 | `app/agent/chat.py::_react_loop` |
| Skill 解析 | `app/agent/skills/loader.py` |
| 配置热更新 | `app/config/` |
