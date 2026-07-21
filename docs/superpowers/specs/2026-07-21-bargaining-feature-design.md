# 议价功能设计（Web 版 · 独立功能）

- 日期：2026-07-21
- 分支：feature/w1-service-streaming
- 状态：已评审通过，待实现

## 1. 背景与目标

现有 Web 版客服（`run_api.py` → `SessionManager` → `EcomAgent` 单 Agent ReAct 流）缺少议价能力。
本功能新增一个独立的**议价**特性：买家针对某商品砍价时，Agent 能按可控的阶梯策略让价，
守住底价、到位则成交、破底则婉拒。

设计原则：**LLM 负责说话，工具负责算钱**。让价金额由确定性纯函数计算，可单测、可复现，
不把定价决策交给模型自由发挥。

非目标（YAGNI）：
- 不接入任何第三方交易平台（闲鱼等），仅在现有合规 Web 通道内演示。
- 不改 Web 前端。
- 不为议价单独引入 multi-agent 子 Agent。

## 2. 总体思路

新增 `negotiate_price` 工具，注册进现有 function-calling 工具表。买家发起砍价时：

1. Agent 先用已有的 `query_product` 拿到 `product_id`；
2. 调用 `negotiate_price(product_id, buyer_offer)`；
3. 工具确定性地算出 `decision`（接受/还价/拒绝）+ `suggested_price`，并更新该会话对该商品的议价轮次；
4. Agent 依据返回结果组织自然话术回复，**不透露底价、不报出低于 `suggested_price` 的价格**。

## 3. 让价阶梯逻辑（工具内纯函数）

输入：标价 `P`、底价 `F`、买家出价 `B`（可为空）、当前已发生轮次 `n`（0 基）、`max_rounds`、`decay`。

- **底价**：`F = floor_price`（该商品在 DB 里设了就用它）`else round(P × bargain_floor_ratio, 2)`（默认系数 0.85）。
- **本轮最低可让价**：`ladder(n) = F + (P − F) × decay^(n+1)`（`decay=0.5`，越砍越接近底价）；当 `n ≥ max_rounds` 时 `ladder = F`。
- **决策规则**（按顺序判定）：
  | 条件 | decision | suggested_price |
  |------|----------|-----------------|
  | `B` 为空（只说"便宜点"，没报数字） | counter | `ladder(n)` |
  | `B ≥ P` | accept | `P` |
  | `F ≤ B` 且 `B ≥ ladder(n)` | accept | `B` |
  | `F ≤ B` 且 `B < ladder(n)` | counter | `ladder(n)` |
  | `B < F` | reject | `F` |
- **不变量**：`suggested_price` 永远 `≥ F`；counter 时 `suggested_price ≤ P`。
- **`floor_hit`**：当 `decision == "reject"` 或 `suggested_price == F`（已让到底价）时为 `true`，否则 `false`。用于 Agent 判断是否该"守死底价"。
- 返回结构：
  ```json
  {
    "product_id": "...", "product_name": "...",
    "list_price": 0.0, "buyer_offer": 0.0,
    "round": 1, "decision": "accept|counter|reject",
    "suggested_price": 0.0, "floor_hit": false,
    "rationale": "内部说明，供 Agent 参考，禁止原样念给买家"
  }
  ```

## 4. 数据层改动（`app/db/`）

- `products` 表新增列 `floor_price REAL`（可空）。
- `init_schema()` 增加迁移守卫：检查 `PRAGMA table_info(products)`，若无 `floor_price` 列则 `ALTER TABLE products ADD COLUMN floor_price REAL`（沿用项目 Xianyu `chat_id` 迁移的做法，兼容旧库）。
- 新表：
  ```sql
  CREATE TABLE IF NOT EXISTS bargain_sessions (
      session_id TEXT NOT NULL,
      product_id TEXT NOT NULL,
      rounds INTEGER DEFAULT 0,
      last_offer REAL,
      updated_at TEXT,
      PRIMARY KEY (session_id, product_id)
  );
  ```
- 新增 DB 方法：
  - `get_bargain_state(session_id, product_id) -> {rounds, last_offer} | None`
  - `bump_bargain_state(session_id, product_id, offer)`：轮次 +1、记录 `last_offer`、更新时间（UPSERT）。
  - `clear_bargain_state(session_id)`：清空该会话所有议价记录。
- `seed.py`：给部分商品填 `floor_price`，另一部分留空以验证系数回退路径。种子来源仍统一走 `mock_data`（在 `PRODUCTS` 增加可选 `floor_price` 字段）。

## 5. 会话状态与并发

- `EcomAgent.__init__` 增加 `session_id` 参数；`SessionManager._default_factory` 一并传入（从 `session_id` 推导 `session_path`）。
- 用 `contextvars.ContextVar`（模块级，如 `_current_session_id`）在 `EcomAgent.chat()` 入口 `set(self.session_id)`；`negotiate_price` 通过 `get(None)` 读取当前会话。
  - 理由：`execute_tool(**arguments)` 只透传 LLM 参数，`session_id` 不能由 LLM 提供；worker 线程内设、同线程内读，天然线程隔离，避免 `set_memory_manager` 那种模块全局在多会话并发下互相串味的问题。
  - 若 `session_id` 为空（如 CLI 直接用 EcomAgent），议价按 `rounds=0` 无状态降级，不报错。
- `SessionManager.reset(session_id)` 内追加 `clear_bargain_state(session_id)`。

## 6. 配置（`app/config/settings.py`）

新增字段（均可由 `.env` 覆盖）：
- `bargain_enabled: bool = True`
- `bargain_floor_ratio: float = 0.85`
- `bargain_max_rounds: int = 5`
- `bargain_decay: float = 0.5`

`bargain_enabled=False` 时：不注册 `negotiate_price` 工具（工具表按开关过滤），Agent 退回普通话术。

## 7. 提示词（`app/prompts/customer_service.py` + 工具 description）

- 工具 `description`：说明"当买家提出具体价格或要求折扣/优惠时调用；调用前需先确定 `product_id`（可用 `query_product` 查）"。
- SYSTEM_PROMPT 增补议价守则：依据 `decision`/`suggested_price` 组织话术；**禁止报出低于 `suggested_price` 的价格；禁止透露底价与 `rationale`；reject 时态度婉转但坚持底价**。

## 8. 与现有能力的交互

- **fast_path**：只匹配寒暄/感谢/告别（≤20 字），不拦截议价，无冲突。
- **guardrails**：议价话术不含站外联系方式，`ContactInfoGuard`/`SensitiveInfoGuard` 不误伤。
- **multi-agent**：工具注册在全局 `TOOL_MAP`，单/多 Agent 均可调用，无需额外改动。
- **observability**：`negotiate_price` 作为工具调用自然进入 Trace。

## 9. 组件边界一览

| 组件 | 职责 | 依赖 |
|------|------|------|
| `app/agent/tools/bargain.py`（新） | 阶梯纯函数 + `negotiate_price` 工具入口 | `db`、`settings`、`contextvars` |
| `app/db/database.py`（改） | `floor_price` 列 + 迁移 + `bargain_sessions` 读写 | sqlite |
| `app/agent/tools/registry.py`（改） | 注册工具与 schema（按 `bargain_enabled` 过滤） | `bargain.py` |
| `app/agent/chat.py`（改） | `session_id` 参数 + `chat()` 绑定 ContextVar | `bargain.py` |
| `app/api/session_manager.py`（改） | 传入 `session_id` + reset 清议价状态 | `db` |
| `app/config/settings.py`（改） | 议价相关配置 | — |
| `app/prompts/customer_service.py`（改） | 议价话术守则 | — |

## 10. 测试

- `tests/test_bargain_ladder.py`：阶梯纯函数——接受/还价/拒绝/触底/无出价/多轮递减/`suggested_price` 永不破底/`max_rounds` 后等于 `F`。
- `tests/test_bargain_db.py`：`bump` 自增轮次、`get` 读取、`clear` 清零；`floor_price` 存在用其值、为空走系数回退。
- `tests/test_bargain_tool.py`：端到端调用 `negotiate_price`（含 ContextVar 注入 `session_id`），验证 decision/suggested_price 与 DB 轮次推进。

## 11. 验收标准

1. 在 Web 聊天里对某商品连续砍价，Agent 逐轮让价、金额单调趋近底价且从不破底。
2. 买家出价 ≥ 某轮阶梯价时成交；出价低于底价时婉拒并守住底价。
3. 设了 `floor_price` 的商品用其底价，未设的按 `标价 × 0.85`。
4. 不同会话/不同商品的议价轮次互不干扰；`reset` 后轮次清零。
5. 全部新增单测通过，且不破坏现有测试。
