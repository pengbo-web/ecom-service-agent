# 共享向量记忆池 实施方案（基于已接入的 ApeRAG）

**Goal:** 让三个 Agent 共享一个**会随使用变聪明**的长期记忆——把「历史成功话术」
「失败教训」「参谋洞察」沉淀成可语义检索的经验库，而不是每次都从零推理。

**前提结论（本方案成立的基础，已核过代码）:** ApeRAG **有完整的写入面**，可以直接
当这个池子用，不需要再引入 Milvus / Qdrant / pgvector。

---

## 一、ApeRAG 能力核查（读 `D:\2026项目\ApeRAG` 得出，不是推测）

| 能力 | 接口 | 现状 |
|---|---|---|
| 语义检索 | `POST /collections/{id}/searches` | ✅ **项目已在用**（`external_kb.aperag_search`） |
| **写入文档** | `POST /collections/{id}/documents/upload`（multipart） | ✅ 有，项目**未接** |
| **确认入库并建索引** | `POST /collections/{id}/documents/confirm` | ✅ 有，项目未接 |
| 删除文档 | `DELETE /collections/{id}/documents/{doc_id}` | ✅ |
| 重建索引 | `.../rebuild_indexes` | ✅ |
| 建 collection | `POST /collections` | ✅ |
| 索引类型 | vector / fulltext / graph / summary / vision | ✅ 五种 |

**三个必须绕开的约束（都来自实际 schema，会直接决定设计）:**

1. **写入是「文件式」不是「插一条记录」。** `upload` 收 multipart 文件。要存一条
   「成功话术」，得把它组织成一份文档（`.md`）上传。**这决定了沉淀的粒度是
   「一份经验文档」而不是「一条对话」。**
2. **索引是异步的。** `UPLOADED → confirm → PENDING → 建索引`。写完**不能立即检索**。
   所以这个池子只能用于「隔一段时间才需要的知识」，绝不能挂在买家会话的热路径上
   等它生效。
3. **检索请求没有元数据过滤。** `searchRequest` 只有 `query` + 各模态参数 +
   `rerank` + `save_to_history`，**没有 filter 字段**（结果里有 `metadata`，
   但查询时过滤不了）。
   → **推论：不同用途必须拆成不同 collection**，靠 collection 隔离而不是靠过滤。

---

## 二、Collection 划分（由约束 3 直接决定）

| collection | 存什么 | 谁写 | 谁读 |
|---|---|---|---|
| `kb`（现有） | 政策/FAQ 文档 | 离线灌库 | 客服（每轮预召回） |
| `experience` | **正样本**：成交/问题解决的对话提炼 | 离线 worker | 客服 |
| `lessons` | **负样本**：转人工/差评对话的教训提炼 | 离线 worker | 客服 |
| `insights` | 参谋诊断的可复用结论 | 协作 worker | 客服 + 营销 |

**为什么不合成一个 collection 用 metadata 区分**：检索时过滤不了，一次查询会把
正负样本混着返回——而「避免重蹈负样本覆辙」恰恰要求两者分开检索、分开使用。

---

## 三、Global Constraints

1. **绝不挂在买家热路径的写入上。** 索引异步 + 网络往返，写入一律走离线 worker。
   买家那一轮只负责把原始数据落进 SQLite（已经在做：`session_archive`）。
2. **PII 必须在入库前处理。** 对话片段含手机号/地址/订单号。项目已有输出侧
   `SensitiveInfoGuard`，但那是**出话**用的；入库要单独走一遍脱敏，且
   **脱敏失败一律不入库**（fail-closed）——这条与全项目 fail-soft 的基调相反，
   是刻意的：一条没脱敏的对话进了向量库，之后每次检索都可能把它捞给别人看。
3. **检索结果不得直接进买家回复。** 它是**给模型的参考**，与 KB 预召回同姿态，
   仍要经既有的输出护栏与 `reply_pipeline` 复核。
4. **检索失败保持非致命**：拿不到经验就按现在的方式回答，与 `recall_kb` 一致。
5. 全部可开关、可回退；中文注释；补齐回归。

---

## Task V1: ApeRAG 写入客户端（打通写入面）

**Files:** `app/agent/recall/external_kb.py` 或新建 `app/knowledge/aperag_writer.py`、测试

- `upload_document(collection_id, filename, content) -> doc_id`（multipart）
- `confirm_documents(collection_id, doc_ids)`
- `delete_document(collection_id, doc_id)`（重写经验时先删后写）
- 复用现有的超时/降级姿态（`aperag_timeout_s`、失败返回 None 记 warning）

**验收**：对着真实 ApeRAG 上传一份 md → confirm → 轮询到索引完成 → 能被
`aperag_search` 检索到。**这一步必须在真服务上验证**，不能只写单测。

---

## Task V2: 经验提炼 worker（决定池子里装什么）

**Files:** `app/scripts/distill_experience.py`(新增)、`app/knowledge/distill.py`、测试

这是本方案**最容易做歪**的一步。原则：**入库的是「提炼后的经验」，不是「原始对话」**。

理由有三：原始对话噪声大、检索出来对模型没有直接可用性；PII 面大；而且一通对话
几十轮，作为检索单元粒度太粗。

流程：

```
session_archive（正样本：成交/无转人工；负样本：转人工/差评）
  → 按语义聚类（复用本轮刚做的 app/agent/skills/clustering.py）
  → 每簇让 LLM 提炼成一份结构化经验文档：
       ## 场景 / ## 有效做法 / ## 应避免 / ## 典型话术
  → 脱敏（fail-closed）
  → upload + confirm 到 experience / lessons collection
```

**正负样本的判定必须确定性**，不能让 LLM 判：
- 正样本：会话内无 HITL 升级 且（有成交 或 无差评）
- 负样本：会话触发过 HITL 升级 或 关联订单有差评

数据来源已经全在库里：`session_archive` / `handoffs` / `reviews` / `orders`。

**幂等**：同一簇重复提炼要覆盖而不是堆积——用稳定的文档名（如
`experience-<簇指纹>.md`），先 delete 再 upload。

---

## Task V3: 检索接入（谁在什么时候读）

**Files:** `app/agent/recall/service.py`、`app/multi_agent/collab.py`、测试

统一召回层已经是「一个入口挂 N 个源」的形态，加两个源即可：

| 源 | 何时召回 | 注入位置 |
|---|---|---|
| `experience` | 客服每轮（与 KB 预召回并发，复用同一次并发窗口） | system prompt 参考段 |
| `lessons` | 同上，但**只在 QU 判定为投诉/售后域时**——闲聊轮不需要教训 | 同上 |
| `insights` | 营销起草时（`_llm_draft` 前） | 起草 prompt |

**延迟预算是硬约束**：客服每轮已经有 KB 预召回（实测 ApeRAG 纯向量路 p90 1.11s）。
再加两路会把首字延迟推高。所以：
- 两路合并成**一次并发**，与现有 KB 预取放在同一个 `ThreadPoolExecutor` 批次；
- 独立超时（建议 ≤ `aperag_timeout_s`），超时即本轮无注入；
- 字符预算独立（类比 `recall_kb_max_chars`），防止挤掉真正重要的 KB 片段。

---

## Task V4: 效果闭环（否则无法判断它有没有用）

**Files:** `app/observability/`、看板

沉淀→检索→是否改善，必须可测量，否则这个池子只是「感觉更聪明」：

- 每轮记录：是否命中经验、命中哪份文档、相似度
- 与既有指标关联：命中经验的轮次，其**转人工率 / 差评率**是否低于未命中的
- 这套数据本身又是下一轮提炼的输入（哪些经验真的有用 → 保留；从不命中的 → 淘汰）

**没有 V4 就不要上 V1–V3。** 一个不能证明自己有用的记忆池，会持续消耗延迟与
token，而没人能判断该不该关掉它。

---

## 四、不做什么（避免被"向量万能"带偏）

**不用它替换现有的自进化 Skill 闭环。** 那条链产出的是可审计、可回滚、带版本与
指纹的 **SKILL.md 流程文档**，还有灰度与看门狗——比"检索出一段相似话术"强得多。
向量在自进化里的正确位置是**输入端的语义聚类**（本轮已完成），不是替代产出物。

**不做「用户行为向量召回高意向用户」。** 前置是行为序列埋点（浏览/停留/加购路径），
本项目没有。没有行为数据，向量化的是空气。这条要先立埋点的项。

**不把 `shared_context` 向量化。** 它的 key 是 `diagnosis:<subject>` 且是**主键**
（`ON CONFLICT(key) DO UPDATE`），按商品覆盖——行数上界是商品数，不随时间增长。
每轮"最近 N 条全注入"始终优于检索。

---

## 五、启动条件与顺序

**现在就可以做 V1**（打通写入面，独立可验证，不影响任何现有行为）。

**V2 需要真实语料**：`session_archive` 目前 84 条（测试数据）。上线后积累到
**几百通真实对话**再跑提炼才有意义——从 84 条测试对话里提炼出的"经验"，
只会把测试数据的偏见固化进生产知识库。

**V3 在 V2 产出第一批经验文档后**，先只接 `experience` 一路观察延迟与命中率，
再决定要不要加 `lessons`。

**V4 与 V3 同期上线**，不能后补。

---

## 六、交付后能说的一句话

> 三个 Agent 共享一个基于 ApeRAG 的长期经验库：成交对话与失败教训经确定性判定
> 分流、语义聚类、LLM 提炼、脱敏后入库，客服在应答前语义召回相关经验，营销在
> 起草前召回参谋的历史洞察。命中与否与转人工率、差评率关联可测，用不上的经验
> 会被淘汰——这是一条能自我验证的闭环，不是一个只会变大的向量库。
