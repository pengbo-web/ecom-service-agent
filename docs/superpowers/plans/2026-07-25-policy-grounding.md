# 政策问答强制检索(修凭记忆编造平台政策) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 修复"政策/流程/规则/时效类问题模型凭记忆直接讲、编造平台政策(实测编出'订单号 YD 开头 16 位',真实是 ORD-)"——两道防线:①三域画像 prompt 硬约束"讲平台政策前**必须先** search_knowledge 或 load_skill,禁止凭记忆讲";②EVALUATOR 接地维度扩展到"政策/规则/格式/费用/时效断言",凭空讲政策判不合格。

**Architecture:** 核心是**强制检索**(修法1)——政策问题一旦被要求先 `search_knowledge`/`load_skill`,就从"简单轮"(不调工具、跳过流水线、无接地校验)变成"复杂轮"(走 ReplyPipeline 的评估),编造随之进入接地校验射程;同时它顺带提高了技能触发率(load_skill 是政策流程的入口)。修法2(EVALUATOR 补政策接地)是二道防线:即便进了评估,也要能识别"政策断言无检索依据"。纯 prompt 改动,零代码逻辑、零新依赖。

**Tech Stack:** `app/prompts/agents.py`(三域 prompt)、`app/prompts/reply_pipeline.py`(EVALUATOR_PROMPT);测试断言 prompt 文本。

## Global Constraints

- **强制检索约束(逐字,加到三域"工具使用原则")**:"涉及平台**政策/规则/流程/时效/费用/是否支持**(如退换货规则、配送时效、会员权益、订单号格式)的问题,**必须先调用 search_knowledge 检索**(或 load_skill 加载对应技能流程),**严禁凭记忆或经验直接讲平台政策**——平台规则以检索结果为准,凭空讲会编错(如订单号格式、天数、运费承担)。"
- **EVALUATOR 接地维度扩展(逐字)**:接地维度新增覆盖"平台政策、规则、流程步骤、费用/时效、格式(如订单号格式)等断言——若草稿讲了具体平台规则但本轮**没有检索/工具结果**支撑,视为编造(不接地),判不合格。"
- **不扩大门控**:不改 ReplyPipeline 的复杂轮门控(`_step_seq>0`);靠强制检索让政策轮自然变复杂轮,而非改门控逻辑(避免误伤纯闲聊)。
- **约束精准**:仅针对"平台政策/规则/流程/时效/费用/格式"类,不影响普通闲聊/寒暄(那些仍简单轮直答)。
- **三域措辞一致**:PRESALE/MIDSALE/AFTERSALE 三段加同一条约束(编号顺延各自现有"工具使用原则")。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 origin,绝不 upstream;`.venv/Scripts/python.exe`;测试不 print emoji。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/prompts/agents.py`(改) | G1 | 三域"工具使用原则"加政策强制检索约束 |
| `app/prompts/reply_pipeline.py`(改) | G1 | EVALUATOR_PROMPT 接地维度扩展到政策断言 |
| `tests/test_policy_grounding.py`(新) | G1 | 断言三域 prompt + evaluator 含约束词 |

---

### Task G1: 政策强制检索约束 + 评估器接地扩展

**Files:**
- Modify: `app/prompts/agents.py`(PRESALE/MIDSALE/AFTERSALE 三段"工具使用原则"末尾各加一条)、`app/prompts/reply_pipeline.py`(EVALUATOR_PROMPT 接地维度)
- Test: `tests/test_policy_grounding.py`(新)

**Interfaces:**
- Consumes: 现有 `PRESALE_PROMPT`/`MIDSALE_PROMPT`/`AFTERSALE_PROMPT`、`EVALUATOR_PROMPT`。
- Produces: 三域 prompt 均含"必须先 search_knowledge/load_skill 检索政策,禁止凭记忆讲"约束;EVALUATOR 接地含政策断言判定。

- [ ] **Step 1: 写失败测试** `tests/test_policy_grounding.py`

```python
"""政策问答强制检索:三域 prompt 硬约束 + evaluator 接地覆盖政策断言。纯文本断言。"""

from app.prompts.agents import PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT
from app.prompts.reply_pipeline import EVALUATOR_PROMPT


def test_all_domains_forbid_unsourced_policy():
    for p in (PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT):
        assert "search_knowledge" in p
        assert "严禁凭记忆" in p or "禁止凭记忆" in p    # 负面硬约束存在
        assert "政策" in p


def test_evaluator_grounding_covers_policy_claims():
    assert "政策" in EVALUATOR_PROMPT
    assert "订单号格式" in EVALUATOR_PROMPT or "格式" in EVALUATOR_PROMPT
    # 明确"无检索支撑的政策断言=不接地"
    assert "没有检索" in EVALUATOR_PROMPT or "无检索" in EVALUATOR_PROMPT or "检索结果" in EVALUATOR_PROMPT
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_policy_grounding.py -q` FAIL

- [ ] **Step 3: agents.py 三域加约束**——在 PRESALE/MIDSALE/AFTERSALE 各自"工具使用原则"最后一条之后,加同一条(编号顺延;下例为 AFTERSALE,现有到第 5 条,加第 6 条;PRESALE/MIDSALE 按各自末号顺延):

```
6. 涉及平台**政策/规则/流程/时效/费用/是否支持**(如退换货规则、配送时效、会员权益、订单号格式)的问题,**必须先调用 search_knowledge 检索**(或 load_skill 加载对应技能流程),**严禁凭记忆或经验直接讲平台政策**——平台规则以检索结果为准,凭空讲会编错(如订单号格式、天数、运费承担)
```

（三段末尾各加此条,措辞一字不差;仅编号按各段现有最后一条顺延。）

- [ ] **Step 4: reply_pipeline.py EVALUATOR 接地维度扩展**——把接地维度第 1 条改为:

```python
1. **接地**：草稿中涉及的订单状态、金额、物流进度、库存等具体事实，是否都能在本轮工具的真实结果中找到依据；有没有脱离工具返回、凭空编造或引用了工具里不存在的数据。**平台政策、规则、流程步骤、费用/时效、格式(如订单号格式)等断言同样受此约束**：若草稿讲了具体平台规则/政策,但本轮没有检索结果(search_knowledge)或工具结果支撑,视为凭空编造(不接地),判不合格。
```

- [ ] **Step 5: 跑通过** → `.venv/Scripts/python.exe -m pytest tests/test_policy_grounding.py tests/test_reply_pipeline.py tests/test_controller_agent.py -q`
Expected: PASS(reply_pipeline 现有测试断言 EVALUATOR 关键词,新增文本不破坏"接地"/"不得改变任何事实"等既有断言)

- [ ] **Step 6: 提交** `feat(prompt): G1 政策问答强制检索 + 评估器接地覆盖政策断言`

---

### Task G2: 端到端冒烟(控制方执行)

- [ ] 重启主服务(MCP server 保持常驻)→ 登录 `123`(无订单)
- [ ] 问"退货完整流程是什么" → **触发 search_knowledge 或 load_skill**(不再纯知识直答);回复基于真实政策库,**不再出现"YD 开头 16 位"这类编造**(真实订单号是 ORD- 开头)
- [ ] 对照负面:纯闲聊"你好呀"→ 仍简单轮直答,不强制检索(约束精准不误伤)
- [ ] Langfuse 该轮:出现 `execute_tool search_knowledge`(或 load_skill),回复接地
- [ ] 复杂轮若模型检索后仍编超出检索的政策 → 评估器打回(二道防线;可选验证)
- [ ] `.superpowers/sdd/progress.md` 记账

## 总量与顺序

G1(~0.4d)→ G2(~0.1d),共 **~0.5 人日**。

## Self-Review

- **覆盖核对**:强制检索约束进三域(G1 test_all_domains)✅;EVALUATOR 接地扩展到政策断言(G1 test_evaluator)✅;强制检索→政策轮变复杂轮→进评估(架构说明,不改门控)✅;约束精准不误伤闲聊(G2 负面对照)✅;顺带提高 load_skill 触发(search_knowledge/load_skill 二选一)✅;修真实 bug(编造 YD 订单号,G2 验证不再编)✅。
- **占位符扫描**:三域加约束"措辞一字不差、仅编号顺延"给了明确规则(实现者读各段末号);EVALUATOR 第 1 条给了完整替换文本;无 TBD。
- **类型一致性**:纯 prompt 文本改动,无签名;测试断言的关键词("严禁凭记忆"/"政策"/"检索结果")与 Step3/4 写入文本一致。
- **已知取舍**:①靠 prompt 强约束而非代码强制(LLM 遵从非 100%,但配合评估二道防线;真正硬保证需"政策意图检测→强制注入检索",过重,YAGNI)②评估器补的政策接地只在复杂轮生效——强制检索让政策轮变复杂轮是前提,两者配套;若模型完全无视约束仍简单轮直答,则评估不介入(prompt 约束是唯一防线),此残留风险记录在案。
