# 检索关键路径与观测补洞 实施方案（第三轮）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** 把首字时间从实测 21–23 秒压下来，方法是**不再为同一轮对话做两次知识检索**，并把这段目前完全不可见的耗时暴露到 trace 里。

**方法:** 每一条都来自这一轮的**实测 trace**，不是推测。

---

## 实测基线（2026-08-08，重启服务后两轮真实请求）

| 指标 | 「鞋子尺码不对想退货」 | 「退货运费谁承担」 |
|---|---|---|
| 首帧 | 0.95 s | — |
| **首字** | **21.53 s** | **20.93 s** |
| 总耗时 | 22.78 s | 23.77 s |

自研 trace 的 span 归因（第二轮）：

```
offset     dur     span
     0   2553ms    llm.chat.create        ← 查询理解
  2554      0      route:小夕-售后
  4432      0      faq_cache
  4477      0      skill_preloaded:process-return
  4478  19194ms    stage:react
 18233      0      recall:kb              ← 检索在这里才结束
 18234   2694ms    llm.chat.create        ← 最终生成
 20934      0      reply_delta:first      ← 首字
```

**4478 → 18233 = 13.75 秒花在 `_build_messages()` 里的知识检索上，占首字时间的 66%，而这段里没有任何 span。**

---

## 根因一：并发预取的结果几乎总是被丢弃，等于每轮检索两次

`orchestrator.chat()` 在查询理解之前提交了一次 KB 预取（用**原始用户输入**发起）。
`EcomAgent.set_turn_kb_prefetch_future` 的文档写明复用条件是：

> 只有当 `_build_messages` 里最终要用的检索 query 与预取时的 query **完全相同**才会被复用；不同则原地丢弃

而查询理解这一步的产出里就包含 `kb_query` **改写**。改写后的串与原句逐字节相同是小概率事件——也就是说：

- 预取那一次几乎必然作废（后台仍会跑完，白花一次 ApeRAG 调用）
- 现场再做一次检索，**这一次是阻塞的**，完整计入首字时间

实测证据：进度帧里 `retrieving` 出现了**两次**（2.84 s 一次、7.57 s 一次），对应两次检索。

并发化非但没有兑现收益，还把本来就慢的 ApeRAG 的并发压力翻了一倍。

---

## 根因二：检索失败的代价被本地兜底翻倍

`.env` 里 `KB_LOCAL_FALLBACK_ENABLED=true`。ApeRAG 超时（6 s）后又走一次本地索引检索
（需要调 embedding 接口，实测约 4–8 s），于是一次失败的检索花掉 13.75 s，
**比直接等 ApeRAG 返回还慢**。

这一项同时和项目所有者已经做出的决定冲突——所有者明确要求"去掉本地 RAG"，
`.env` 没有跟上。

---

## 根因三：这段耗时在 trace 里完全不可见

- `recall:kb` 是一个 `dur_ms=0` 的**事件点**，不是有时长的 span，看不出检索花了多久
- 上一次提交新加的 `kb_latency` 观测事件**没有出现在任何一条 trace 里**
- 普通工具执行不发 `tool_call` 事件（只有需要人工确认的工具发，见 `app/api/streaming.py:240`），
  所以 ReAct 循环内部同样不可见

结果就是：首字时间的 66% 落在观测盲区里，必须靠人肉基准测试才能发现——这一轮就是这么发现的。

---

## Global Constraints

1. **不动安全边界**：consent 门、工作流守卫、HITL 升级、人工审批、输出护栏一律不改。
2. **不修改 ApeRAG**：不改它的部署、配置、collection、索引设置。本轮只改本项目**怎么调用它**。
3. **检索失败必须保持非致命**：买家仍然拿到回复，只是这一轮没有知识注入。
4. 所有性能改动可回退（开关或配置）。
5. **fail-soft 必须留痕**：凡是"失败后降级/丢弃"的路径都要发可观测事件，不能再出现"一个子系统静默空转"。
6. 中文注释与 UI 文案；只在 `feature/w1-service-streaming` 提交，不建分支不推送。

---

## Task R1: 让并发预取真正被用上（最大的一条）

**Files:** `app/agent/chat.py`、`app/multi_agent/orchestrator.py`、`app/config/settings.py`、`tests/test_qu_recall_concurrency.py`

**要点：**

- 现状：复用条件是"改写后的 kb_query 与原句逐字节相同"，实际几乎永不成立。
- 目标：**一轮对话最多只发生一次阻塞检索**。
- 需要实施者自己判断并在报告里论证的设计点：改写后的 `kb_query` 与原句检索结果的差距，
  是否值得为它付一次完整的阻塞检索。可选方向（不限于）：
  - 默认复用预取结果，只有当查询理解判定 `need_kb=False` 时丢弃（丢弃是省事，不是花钱）；
  - 或者：改写差异大到一定程度才补检索，且补检索**不阻塞首字**（用它更新后续轮次的上下文）。
- **不能**用"把预取推迟到 QU 之后"来解决——那等于取消并发，回到串行。
- 无论选哪种，`need_kb=False` 时都不得把预取结果注入 prompt（门控语义不能被这次优化破坏）。
- 丢弃预取时必须发一条可观测事件（含丢弃原因），这是本轮"fail-soft 留痕"约束的直接应用——
  正是因为丢弃是静默的，这个缺陷才活到今天。
- 保留总开关 `qu_recall_concurrent_enabled`，关掉回到串行。

**验收：** 同一轮请求里对 ApeRAG 的调用次数从 2 次降到 1 次（用打桩计数）；
进度帧里 `retrieving` 不再出现两次；首字时间给出前后实测对比。

---

## Task R2: 检索耗时进 trace

**Files:** `app/agent/chat.py`、`app/agent/recall/`、`app/observability/tracer.py`、`app/observability/langfuse_bridge.py`、测试

**要点：**

- 先查清上一次提交加的 `kb_latency` 事件为什么没落进 trace（代码里有，实际 trace 里没有）——
  **报告里必须说明真正的原因**，不要绕开它另开一条新通道。
- `recall:kb` 现在是 0 ms 的事件点，改成能反映真实耗时的 span，覆盖整个检索段
  （含 ApeRAG 调用、超时等待、以及降级路径）。
- ReAct 循环内部的每次 LLM 调用与每次工具执行都要有 span。当前只有最终生成那次
  有 `llm.chat.create`，工具执行完全没有——一次真实对话里最不透明的一段恰恰是这里。
- 走既有的 tracer + Langfuse 桥两条通道，不新开第三条。
- 注意跨线程：并发预取跑在别的线程上，ContextVar 不会自动传过去；
  事件要落在有正确 trace 上下文的地方（上一次提交在这一点上踩过坑，读它的报告
  `.superpowers/sdd/task-aperag-latency-report.md` 再动手）。

**验收：** 跑一轮真实请求，`/api/traces/{id}` 返回的 span 能把首字时间**逐段**解释清楚，
不再有超过 1 秒的无 span 空白；检索超时的那一轮，trace 里能看出"超时"而不是只看到一段空白。

---

## Task R3: 对齐"去掉本地 RAG"的决定

**Files:** `.env`（不进版本库）、`app/config/settings.py`、`tests/conftest.py`、`tests/test_recall_kb.py`

**要点：**

- `.env` 的 `KB_LOCAL_FALLBACK_ENABLED=true` 与所有者已做的决定冲突，且让一次失败的检索
  从 6 s 变成 13.75 s。改为关闭。
- **`.env` 不进版本库，也不要把其中任何密钥写进代码、测试、报告或提交信息。**
- 上一次提交的报告里记了一个遗留问题：`conftest.py` 没有把 `.env` 的
  `KB_LOCAL_FALLBACK_ENABLED` 重置掉，导致 `tests/test_recall_kb.py` 有一条测试的结果
  取决于本机 `.env`。测试不该读开发机的 `.env`——一并修掉。
- 关掉兜底之后，ApeRAG 超时就等于这一轮零知识注入。实测 ApeRAG 当前分布为
  中位 3.35 s / p90 6.97 s / 最慢 8.16 s（12 次调用，4 个真实问句），
  而当前超时是 6.0 s——**这个组合下会有可观比例的轮次拿不到知识**。
  实施者需要按这个实测分布重新给出超时值并论证，注意两个方向的代价都是真实的：
  太紧=经常零知识，太松=买家干等。

**验收：** 测试结果不再依赖本机 `.env`；关掉兜底后跑真实请求仍能正常回答；
超时值的选择在报告里有实测数据支撑。

---

## 验收（做完我再跑一轮真实体验对比）

1. 同一句「鞋子尺码不对想退货」，**首字时间**给出前后对比数字。
2. 一轮对话对 ApeRAG 的调用次数 = 1。
3. trace 能逐段解释首字时间，无 > 1 s 的观测空白。
4. 进度帧仍严格单调，`retrieving` 不重复出现。
5. 检索超时时买家仍拿到回复，且 trace 里看得出是超时。
6. 全量 pytest 与基线一致；前端全绿 + build 干净。
