# 协作总线路由规则化 实施方案（把编排决策从 Expert 里搬出来）

**Goal:** 让「什么事件该给谁处理」成为**总线层的一份可配置订阅表**，而不是散在各个
Expert 处理器代码里的硬编码。做完之后，新增一个 Agent 只需加一条订阅规则，
**不改任何既有 Expert 的代码**。

**范围:** 只动"路由决策放在哪"。不动 Agent 的能力边界、不动人工闸、不动幂等语义。

---

## 一、现状:传输解耦了，决策没解耦

企业级多智能体架构里，三个 Expert Agent 应当是**平级**的，彼此**严禁越级指挥**；
「谁该接手下一步」由总线层的规则引擎决定，而不是由上一个 Agent 决定。

本项目的总线做对了传输(持久化、幂等认领、correlation_id 可回溯)，但**决策仍在
发送方手里**：

```python
# app/api/streaming.py:49  —— 客服侧代码指定了收件人
bus.publish(EV_SIGNAL_ANOMALY, {...}, AGENT_SERVICE, AGENT_ANALYST)

# app/multi_agent/collab.py:28,181  —— 参谋的处理器里写死了"什么诊断该转给营销"
MARKETING_WORTHY = {"refund_rate_high"}
if not degraded and anomaly.get("kind") in MARKETING_WORTHY:
    bus.publish(EV_INSIGHT_DIAGNOSIS, diagnosis, AGENT_ANALYST, AGENT_GROWTH)
```

且 `agent_events.target_agent` 是 `NOT NULL` —— 每条事件的收件人在发布那一刻就被
钉死。这是**经总线中转的点对点**，不是订阅制。

具体代价（不是理论上的）：

1. **加一个 Agent 要改既有 Expert 的代码。** 想加"库存预警 Agent"订阅
   `signal.anomaly`？得改 `streaming.py` 与 `anomaly.py` 的发布调用——而这两处
   一个在买家会话热路径上、一个在扫描器里，都与库存预警毫无关系。
2. **一条事件只能有一个消费者。** `target_agent` 是单值，天然做不到"同一条异常
   信号同时给参谋和库存预警"。
3. **编排规则不可见、不可配。** `MARKETING_WORTHY` 这个决定"营销 Agent 什么时候
   被唤醒"的规则，藏在参谋处理器的模块常量里，运营看不到，改它要发版。

---

## 二、目标形态

```
发布方：只声明「发生了什么」          bus.publish(EV_SIGNAL_ANOMALY, payload, source=AGENT_SERVICE)
路由表：决定「谁该处理」+ 条件         signal.anomaly → [analyst], insight.diagnosis → [growth] if ...
消费方：按订阅表反查自己该收什么       bus.consume(AGENT_ANALYST, handler)
```

发布方**不再指定 target**。路由在 `publish` 内部由订阅表**扇出**成一条或多条投递
记录——同一个事件可以有 0 个、1 个或 N 个订阅者，这三种情况都合法。

---

## 三、Global Constraints

1. **不动安全边界**：consent 门、工具子集不相交、人工审批闸、营销只产草稿——
   全部不改。路由规则决定的是"谁被唤醒"，**永远不能决定"谁被允许做什么"**。
   这条要写进代码注释：路由表是调度，不是授权。
2. **幂等语义不变**：`claim_events` 的条件更新、`finish_event`、
   `reclaim_stale_events` 一律不动。扇出产生的每条投递记录各自独立认领。
3. **向后兼容**：`publish(..., target=...)` 的旧签名必须继续可用（显式 target
   时跳过路由表直投）。理由不是照顾调用方，而是**保留一个逃生口**：将来出现
   "这条事件就是要点对点给某个 Agent"的场景时，不必为它扭曲路由表。
4. **存量事件不迁移**：库里已有的 `agent_events` 行保持原样，`target_agent`
   字段保留。
5. 中文注释；补齐回归；全量套件保持全绿。

---

## 四、设计:扇出发生在写入时，不是读取时

这是本方案唯一需要认真取舍的地方。

**方案 A（读时匹配）**：事件只存一行不带 target，`consume(agent)` 时按路由表反查
"我该收哪些 event_type"，再去捞。
- 优点：写入轻，一条事件一行。
- 致命缺点：**幂等崩了**。同一行事件被两个 Agent 认领，而 `status` 是行级单值——
  参谋处理完置 done，营销就再也捞不到了。要修就得给每个 (event, agent) 单独记
  状态，等于在读路径上重建一张投递表。

**方案 B（写时扇出，采纳）**：`publish` 时按路由表算出订阅者列表，**为每个订阅者
插一行**投递记录（同一 `correlation_id`、同一 payload、不同 `target_agent`）。
- 幂等、认领、回收、失败隔离**全部沿用现有机制，一行不改**——每条投递记录就是
  今天的一条事件。
- 代价：一条事件 N 个订阅者就是 N 行。在本项目的量级下完全可以接受，而且
  时间线上能如实看到"这条信号分发给了谁"，反而更可审计。

**结论：采纳 B。** 它把新增的复杂度全部收敛在 `publish` 一个函数里，读路径零改动——
这正是"不动幂等语义"这条约束的兑现方式。

---

## 五、路由表放在哪:代码常量，不是数据库表

**不建 `subscriptions` 表。** 理由：

- 路由规则的条件（如"degraded 的诊断不转营销"）是**带业务语义的谓词**，不是
  能塞进 SQL 的标量比较。硬塞进表就要发明一套条件 DSL，那是给自己造语言。
- 这份表改动频率极低（加 Agent 才动），而每次改动都必须配一次回归测试——
  这正是代码而非配置的适用场景。
- 放代码里仍然完全兑现了目标：**规则集中在一处、Expert 代码里不再有路由决策**。
  "可配置"的价值在于"集中且可见"，不在于"能热改"。

放在 `app/multi_agent/routing.py`，形态：

```python
SUBSCRIPTIONS: dict[str, list[Subscription]] = {
    EV_SIGNAL_ANOMALY:    [Subscription(AGENT_ANALYST)],
    EV_INSIGHT_DIAGNOSIS: [Subscription(AGENT_GROWTH, when=_marketing_worthy)],
    EV_DRAFTS_READY:      [Subscription(AGENT_HUMAN)],
    ...
}
```

`when` 是 `(payload) -> bool` 的纯函数谓词，`None` 表示无条件订阅。
`MARKETING_WORTHY` 那条规则从 `collab.py` **搬进来**（不是复制）。

---

## Task R1: 路由表模块

**Files:** `app/multi_agent/routing.py`(新增)、`tests/test_collab_routing.py`(新增)

- `Subscription(target, when=None)` 数据类
- `SUBSCRIPTIONS` 表，覆盖现有全部 6 个事件类型
- `resolve(event_type, payload) -> list[str]`：算出订阅者列表
  - 未登记的 event_type → 返回 `[]`（**不报错**：发布一个没人订阅的事件是合法的，
    "暂时没人关心"与"配置错了"在运行时无法区分，报错只会让新事件类型的引入变成
    一次故障）
  - `when` 谓词抛异常 → 该订阅**不投递**并记 warning（fail-closed：判不清就不唤醒，
    与 `arbitration` 的 fail-closed 同一姿态。多唤醒一个 Agent 的代价是它可能对着
    半成品数据产出垃圾草稿）
- 纯函数、无 IO，便于单测

**验收:** 每个事件类型的订阅者可断言；`when` 抛异常时不投递且不外泄。

---

## Task R2: publish 支持扇出

**Files:** `app/multi_agent/bus.py`、`tests/test_collab_routing.py`

- `publish(event_type, payload, source, target=None, correlation_id=None)`
  - `target` 显式给 → 保持今天的行为（单行直投，逃生口）
  - `target=None` → 走 `routing.resolve()`，为每个订阅者插一行
- 返回值语义**不变**：仍返回 `correlation_id`（失败/无订阅者时返回 None）
  - **注意**：无订阅者返回 None 与"发布失败返回 None"撞在一起。调用方现有代码
    只用它做真值判断（见 `anomaly.py:182` 的 `if bus.publish(...)`），语义上
    "没人订阅"确实等同于"这条链没起来"，可以合并。但要在 docstring 里写明，
    免得后来者以为 None 一定是故障。
- fail-soft 不变：任何异常吞掉记日志，绝不影响买家热路径

**验收:** 单订阅者行为与今天逐字节一致；多订阅者插多行且 correlation_id 相同；
无订阅者不插行不抛错；显式 target 绕过路由表。

---

## Task R3: 五个发布点去掉 target

**Files:** `app/api/streaming.py`、`app/agent/tools/anomaly.py`、
`app/multi_agent/collab.py`、`app/api/app.py`、`app/scripts/attribute_outreach.py`

逐个改成不传 target。**重点是 `collab.handle_signal`**：

```python
# 改前:参谋的处理器自己判断"该不该转给营销"并指定收件人
if not degraded and anomaly.get("kind") in MARKETING_WORTHY:
    bus.publish(EV_INSIGHT_DIAGNOSIS, diagnosis, AGENT_ANALYST, AGENT_GROWTH)

# 改后:参谋只宣布"我出了一条诊断",转不转、转给谁由路由表决定
corr = bus.publish(EV_INSIGHT_DIAGNOSIS, diagnosis, AGENT_ANALYST, correlation_id=corr)
```

`degraded` 与 `kind` 都已经在 `diagnosis` payload 里，谓词拿得到——**这是能搬的
前提**，改前要先确认（已确认：`diagnosis` 含 `kind` 与 `degraded` 两个键）。

`forwarded` 这个返回字段的语义随之变化：从"我决定转了"变成"路由表判定有下游"。
保留字段名（外部有断言），但注释要写清语义已变。

---

## Task R4: 时间线与健康出口体现扇出

**Files:** `app/api/app.py`(timeline 端点)、前端(可选)

一条信号扇出成 N 行之后，时间线上会出现 N 条同 `correlation_id`、同 `event_type`、
不同 `target_agent` 的记录。这**不是重复**，是"分发给了谁"的如实记录——但界面上
必须能看出来，否则运营会以为是 bug。

时间线返回里已经有 `target_agent` 字段，前端补一个"→ 收件人"的展示即可。

---

## 刻意不做

**不建数据库配置表、不做热更新**（理由见第五节）。
**不做订阅优先级/顺序**：扇出的多个订阅者天然并行、互不依赖；一旦引入顺序，就等于
在总线上重建了工作流编排——那正是这套架构要避开的东西。
**不改 `target_agent` 字段为可空**：扇出后每行仍然有确定的收件人，这个字段的语义
反而更清晰了（它现在表示"这条投递记录给谁"，而不是"发送方指定给谁"）。

---

## 交付后能说的一句话

> 协作总线采用发布/订阅 + 总线层路由表：Expert Agent 之间无直接调用、无越级指挥，
> 发布方只声明「发生了什么」，「谁该处理」由总线的订阅规则决定（含业务条件谓词）。
> 新增一个 Agent 只需加一条订阅规则，不改任何既有 Agent 的代码。
