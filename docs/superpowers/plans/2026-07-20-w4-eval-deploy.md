# W4：评估回归 + Trace 回流 + 容器化部署 + 收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收官四件事：① 把现有评估升级为**回归门禁**（与 baseline 对比，掉点则 `exit 1`，可接 CI）；② **线上 Trace 回流**成评估用例（被拦截/转人工/出错的真实对话沉淀为回归测试）；③ **容器化部署**（Dockerfile + docker-compose，借鉴 XianyuAutoAgent）；④ **收尾**（README 架构图 + 面试逐字稿）。

**Architecture:** 回归比较是**纯函数**（`compare_to_baseline`），建立在现有 `Evaluator.run_all` 产出的 `summary` 上，离线可测；实际评估运行沿用现有 `run_eval.py`（需 API）。Trace 回流的转换是**纯函数**（`trace_to_case`），从 `TraceStore.get_trace` 的字典构造 `EvalCase` 字段，离线可测；配一个 CLI 把问题 Trace 追加进回流数据集。Docker 与文档为文件产出 + 人工验证。核心零改动。

**Tech Stack:** Python 3.11+、现有 evaluation 模块、Docker、pytest（纯函数离线测）。

## Global Constraints

- **Python 3.11+**；解释器 `D:/2026项目/ecom-service-agent/.venv/Scripts/python.exe`。
- **核心零改动**：只在 `app/evaluation/`、`app/scripts/`、根目录（Docker/README/docs）加东西。
- **回归比较与 Trace 转换必须是纯函数、离线可测**（不跑 LLM、不连网络）。
- **不破坏既有评估**：`run_eval.py` 原有行为保留，新增 `--regression` / `--save-baseline` 为可选开关。
- **Docker 面向生产**：非 root 运行、依赖分层缓存、`.dockerignore` 排除 venv/session/db。
- **baseline 与回流数据可入库/被 .gitignore**：baseline 建议提交（作为质量基线），回流生成物写入 sessions（gitignore）。
- **Windows 友好**：脚本用 `pathlib`。

---

## File Structure

- `app/config/settings.py` — 修改：新增 `eval_baseline_path`、`eval_regression_tolerance`。
- `app/evaluation/regression.py` — 新建：`compare_to_baseline` / `save_baseline` / `load_baseline`。
- `app/evaluation/trace_to_case.py` — 新建：`trace_to_case`（Trace dict → EvalCase 字段 dict）。
- `app/scripts/run_eval.py` — 修改：加 `--regression` / `--save-baseline`。
- `app/scripts/reflow_traces.py` — 新建：拉问题 Trace → 转用例 → 追加到回流数据集。
- `Dockerfile` — 新建。
- `docker-compose.yml` — 新建。
- `.dockerignore` — 新建。
- `README.md` — 修改：架构图 + 部署说明。
- `docs/面试逐字稿.md` — 新建：项目讲解逐字稿。
- `tests/test_regression.py` / `tests/test_trace_to_case.py` — 新建。

---

## Task 1: 回归比较逻辑 + baseline 存取

**Files:**
- Modify: `app/config/settings.py`
- Create: `app/evaluation/regression.py`
- Test: `tests/test_regression.py`

**Interfaces:**
- Produces:
  - `settings.eval_baseline_path: str="app/evaluation/baseline.json"`、`eval_regression_tolerance: float=0.05`
  - `regression.py`：
    - `compare_to_baseline(current: dict, baseline: dict, tolerance: float=0.05) -> dict`（入参为两个 summary；返回 `{"regressed": bool, "diffs": [{"metric","baseline","current","delta","regressed"}]}`；只比较三项：`pass_rate`/`avg_process_score`/`avg_result_score`；baseline 或 current 该项为 None 则跳过该项）
    - `save_baseline(summary: dict, path) -> None`
    - `load_baseline(path) -> Optional[dict]`（不存在返回 None）

- [ ] **Step 1: 写失败测试**

`tests/test_regression.py`：
```python
from app.evaluation.regression import compare_to_baseline, save_baseline, load_baseline


def test_no_regression_when_equal():
    s = {"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.85}
    r = compare_to_baseline(s, s, tolerance=0.05)
    assert r["regressed"] is False


def test_regression_when_drop_exceeds_tolerance():
    base = {"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.85}
    cur = {"pass_rate": 0.7, "avg_process_score": 0.8, "avg_result_score": 0.85}  # 掉 0.2
    r = compare_to_baseline(cur, base, tolerance=0.05)
    assert r["regressed"] is True
    diffs = {d["metric"]: d for d in r["diffs"]}
    assert diffs["pass_rate"]["regressed"] is True


def test_small_drop_within_tolerance_ok():
    base = {"pass_rate": 0.90, "avg_process_score": 0.80, "avg_result_score": 0.85}
    cur = {"pass_rate": 0.87, "avg_process_score": 0.80, "avg_result_score": 0.85}  # 掉 0.03
    assert compare_to_baseline(cur, base, tolerance=0.05)["regressed"] is False


def test_improvement_not_regression():
    base = {"pass_rate": 0.7, "avg_process_score": 0.7, "avg_result_score": 0.7}
    cur = {"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.9}
    assert compare_to_baseline(cur, base, tolerance=0.05)["regressed"] is False


def test_none_metrics_skipped():
    base = {"pass_rate": 0.9, "avg_process_score": None, "avg_result_score": 0.8}
    cur = {"pass_rate": 0.9, "avg_process_score": None, "avg_result_score": 0.8}
    r = compare_to_baseline(cur, base, tolerance=0.05)
    assert r["regressed"] is False
    assert all(d["metric"] != "avg_process_score" for d in r["diffs"])


def test_save_and_load_baseline(tmp_path):
    p = tmp_path / "baseline.json"
    assert load_baseline(p) is None
    save_baseline({"pass_rate": 0.9}, p)
    assert load_baseline(p)["pass_rate"] == 0.9
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_regression.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.evaluation.regression'`。

- [ ] **Step 3: 实现**

`app/config/settings.py` 在 Evaluation 配置块附近加：
```python
    eval_baseline_path: str = "app/evaluation/baseline.json"
    eval_regression_tolerance: float = 0.05  # 单指标允许的最大回退幅度
```
`app/evaluation/regression.py`：
```python
"""评估回归门禁：与 baseline 对比，掉点超过容差即判回退。"""

import json
from pathlib import Path
from typing import Optional

_METRICS = ["pass_rate", "avg_process_score", "avg_result_score"]


def compare_to_baseline(current: dict, baseline: dict, tolerance: float = 0.05) -> dict:
    diffs = []
    regressed = False
    for m in _METRICS:
        b = baseline.get(m)
        c = current.get(m)
        if b is None or c is None:
            continue
        delta = c - b
        m_reg = delta < -tolerance
        if m_reg:
            regressed = True
        diffs.append({"metric": m, "baseline": b, "current": c,
                      "delta": delta, "regressed": m_reg})
    return {"regressed": regressed, "diffs": diffs}


def save_baseline(summary: dict, path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def load_baseline(path) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_regression.py -v`
Expected: PASS（6 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/config/settings.py app/evaluation/regression.py tests/test_regression.py
git commit -m "feat(eval): 回归比较逻辑 + baseline 存取"
```

---

## Task 2: run_eval 接入回归门禁

**Files:**
- Modify: `app/scripts/run_eval.py`
- Test: 人工（需 API）

**Interfaces:**
- Consumes: `compare_to_baseline` / `save_baseline` / `load_baseline`（Task 1）。
- Produces: `run_eval.py` 新增 `--save-baseline`（把本次 summary 存为 baseline）与 `--regression`（与 baseline 对比，掉点则 `sys.exit(1)`）。

- [ ] **Step 1: 实现**

`app/scripts/run_eval.py`：
- import 追加：
```python
from app.evaluation.regression import compare_to_baseline, save_baseline, load_baseline  # noqa: E402
```
- argparse 追加：
```python
    parser.add_argument("--save-baseline", action="store_true",
                        help="将本次评估 summary 存为回归基线")
    parser.add_argument("--regression", action="store_true",
                        help="与基线对比，掉点超过容差则以非零码退出（CI 门禁）")
```
- 在 `_print_report(report)` 之后、`if args.output` 之前插入：
```python
    baseline_path = ROOT / settings.eval_baseline_path
    if args.save_baseline:
        save_baseline(report["summary"], baseline_path)
        print(f"\n✅ 已保存回归基线: {baseline_path}")

    if args.regression:
        baseline = load_baseline(baseline_path)
        if baseline is None:
            print(f"\n⚠️  无基线可比（先跑 --save-baseline）: {baseline_path}")
        else:
            cmp = compare_to_baseline(report["summary"], baseline,
                                      settings.eval_regression_tolerance)
            print("\n" + "=" * 78)
            print("  回归门禁（vs 基线）")
            print("=" * 78)
            for d in cmp["diffs"]:
                flag = "❌ 回退" if d["regressed"] else "✅"
                print(f"  {d['metric']:<20} 基线 {d['baseline']:.3f} → 本次 "
                      f"{d['current']:.3f}  (Δ {d['delta']:+.3f}) {flag}")
            if cmp["regressed"]:
                print("\n❌ 检测到质量回退，评估门禁未通过。")
                sys.exit(1)
            print("\n✅ 未见回退，评估门禁通过。")
```

- [ ] **Step 2: 冒烟（不需 API，仅验证 --help 与 import 正常）**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m app.scripts.run_eval --help`
Expected: 输出含 `--save-baseline` 与 `--regression`，无 import 错误。

- [ ] **Step 3: 提交**

```bash
git add app/scripts/run_eval.py
git commit -m "feat(eval): run_eval 加 --save-baseline / --regression 门禁"
```

---

## Task 3: Trace 回流成用例

**Files:**
- Create: `app/evaluation/trace_to_case.py`
- Create: `app/scripts/reflow_traces.py`
- Test: `tests/test_trace_to_case.py`

**Interfaces:**
- Consumes: `TraceStore`（W2）的 trace dict 结构（`trace_id/user_input/intent/status/spans`）。
- Produces:
  - `trace_to_case.py`：`trace_to_case(trace: dict) -> dict`（返回 EvalCase 字段 dict：`id/description/turns` 必有；有工具 span→`expected_tools`；有 hitl span→`expected_requires_human=True`；intent 有效→`expected_intent`）；`is_problem_trace(trace) -> bool`（error / intent=="blocked" / 含 hitl span）
  - `reflow_traces.py`：CLI，从 TraceStore 拉问题 Trace → 转用例 → 追加写入回流数据集 JSON。

- [ ] **Step 1: 写失败测试**

`tests/test_trace_to_case.py`：
```python
from app.evaluation.trace_to_case import trace_to_case, is_problem_trace


def _trace(**over):
    t = {"trace_id": "abc12345", "user_input": "我的订单发货了吗", "intent": "order_query",
         "status": "ok", "spans": []}
    t.update(over)
    return t


def test_basic_case_fields():
    c = trace_to_case(_trace())
    assert c["turns"] == ["我的订单发货了吗"]
    assert "abc12345" in c["id"]
    assert c["expected_intent"] == "order_query"


def test_tool_spans_become_expected_tools():
    t = _trace(spans=[{"kind": "tool", "name": "tool:query_order"},
                      {"kind": "llm", "name": "llm.chat.create"}])
    c = trace_to_case(t)
    assert c["expected_tools"] == ["query_order"]


def test_hitl_span_sets_requires_human():
    t = _trace(spans=[{"kind": "hitl", "name": "handoff"}])
    assert trace_to_case(t)["expected_requires_human"] is True


def test_blocked_intent_not_used_as_expected():
    c = trace_to_case(_trace(intent="blocked"))
    assert "expected_intent" not in c


def test_is_problem_trace():
    assert is_problem_trace(_trace(status="error")) is True
    assert is_problem_trace(_trace(intent="blocked")) is True
    assert is_problem_trace(_trace(spans=[{"kind": "hitl", "name": "handoff"}])) is True
    assert is_problem_trace(_trace()) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_trace_to_case.py -v`
Expected: FAIL —— `ModuleNotFoundError`。

- [ ] **Step 3: 实现**

`app/evaluation/trace_to_case.py`：
```python
"""线上 Trace 回流成评估用例（问题对话沉淀为回归测试）。"""


def is_problem_trace(trace: dict) -> bool:
    if trace.get("status") == "error":
        return True
    if trace.get("intent") == "blocked":
        return True
    if any(s.get("kind") == "hitl" for s in trace.get("spans", [])):
        return True
    return False


def trace_to_case(trace: dict) -> dict:
    tid = str(trace.get("trace_id", ""))[:8]
    user_input = trace.get("user_input", "")
    case = {
        "id": f"reflow-{tid}",
        "description": f"线上回流: {user_input[:20]}",
        "turns": [user_input],
    }
    intent = trace.get("intent")
    if intent and intent not in ("blocked", "unknown"):
        case["expected_intent"] = intent

    tools = [s["name"].split(":", 1)[1]
             for s in trace.get("spans", [])
             if s.get("kind") == "tool" and ":" in s.get("name", "")]
    if tools:
        case["expected_tools"] = tools

    if any(s.get("kind") == "hitl" for s in trace.get("spans", [])):
        case["expected_requires_human"] = True

    return case
```
`app/scripts/reflow_traces.py`：
```python
"""把线上问题 Trace（出错/被拦截/转人工）回流成评估用例。

用法：python -m app.scripts.reflow_traces [--limit 200] [--out app/sessions/reflow_cases.json]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from app.observability.store import TraceStore  # noqa: E402
from app.evaluation.trace_to_case import trace_to_case, is_problem_trace  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="线上问题 Trace 回流成评估用例")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default="app/sessions/reflow_cases.json")
    args = ap.parse_args()

    store = TraceStore()
    recent = store.recent_traces(limit=args.limit)
    cases = []
    for row in recent:
        full = store.get_trace(row["trace_id"])
        if full and is_problem_trace(full):
            cases.append(trace_to_case(full))

    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"回流 {len(cases)} 条问题用例 → {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_trace_to_case.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/evaluation/trace_to_case.py app/scripts/reflow_traces.py tests/test_trace_to_case.py
git commit -m "feat(eval): 线上 Trace 回流成评估用例 + reflow CLI"
```

---

## Task 4: 容器化部署

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`
- Test: 人工（`docker build` / `docker compose up`）

**Interfaces:**
- Produces: 可 `docker compose up` 启动服务（映射 8010，读 `.env`，挂载 sessions 卷做持久化）。

- [ ] **Step 1: 写 Dockerfile**

`Dockerfile`：
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 依赖分层缓存：先装依赖，再拷代码
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY run_api.py ./
COPY mcp_server/ ./mcp_server/

# 非 root 运行
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8010

CMD ["python", "run_api.py"]
```

- [ ] **Step 2: 写 compose 与 dockerignore**

`docker-compose.yml`：
```yaml
services:
  ecom-agent:
    build: .
    ports:
      - "8010:8010"
    env_file:
      - .env
    environment:
      - API_HOST=0.0.0.0        # 容器内需监听 0.0.0.0 才能被宿主访问
    volumes:
      - ./app/sessions:/app/app/sessions   # 会话/DB/Trace 持久化
    restart: unless-stopped
```
`.dockerignore`：
```
.venv/
__pycache__/
*.pyc
.git/
.idea/
app/sessions/
docs/
tests/
*.md
```

- [ ] **Step 3: 人工验证（如本机有 Docker）**

```bash
docker compose build
docker compose up -d
curl http://127.0.0.1:8010/api/health   # {"status":"ok"}
docker compose down
```
> 说明：容器内构建业务库/知识库索引可在首次进入容器执行 `python -m app.scripts.init_db` 与 `build_kb_index`，或在 compose 加初始化步骤（本任务先保证服务能起）。若本机无 Docker，跳过实测，仅提交文件。

- [ ] **Step 4: 提交**

```bash
git add Dockerfile docker-compose.yml .dockerignore
git commit -m "feat(deploy): 容器化(Dockerfile + docker-compose + dockerignore)"
```

---

## Task 5: 收尾（README 架构图 + 面试逐字稿）

**Files:**
- Modify: `README.md`（加二次开发后的架构总览 + 部署）
- Create: `docs/面试逐字稿.md`
- Modify: `docs/二次开发规划-生产化改造设计.md`（勾选完成项，可选）

- [ ] **Step 1: README 加"生产化架构总览"**

在 README 合适位置插入一段 ASCII 架构图与能力清单：
```markdown
## 生产化架构总览（二次开发后）

​```
浏览器（聊天 / 看板 / 坐席 单页）
      │  SSE 流式
┌─────▼───────────────────────────────────────────┐
│ FastAPI 服务层                                    │
│  限流 → 人工接管 → 规则快路径 → 成本上限 → Agent   │
│  ├─ Guardrails 护栏（输入注入拦截 / 输出脱敏）      │
│  ├─ Observability（Trace/Span → SQLite → 看板）    │
│  ├─ HITL（升级判定 → 坐席队列 → 人工接管）          │
│  └─ 管理接口鉴权（令牌）                            │
├──────────────────────────────────────────────────┤
│ 核心 Agent（未改动）：ReAct + 工具 + RAG + Memory   │
├──────────────────────────────────────────────────┤
│ 真实数据层：SQLite（商品/订单/物流/用户）           │
└──────────────────────────────────────────────────┘
质量闭环：线上 Trace → 回流用例 → 评估回归门禁
​```

一键部署：`docker compose up -d`（映射 8010，持久化 sessions）。
质量门禁：`python -m app.scripts.run_eval --regression`。
```

- [ ] **Step 2: 写面试逐字稿**

`docs/面试逐字稿.md`：包含
- 一句话项目介绍（真实可上线的电商客服 Agent 系统）
- 架构与关键设计决策（不动核心 / 透明代理埋点 / 规则+LLM 混合 / 质量闭环）
- 每个模块"要解决的面试题"+ 你的答法（护栏防注入、可观测性、转人工、评估回归、成本控制）
- 亮点数据（如快路径秒回、限流精确、拦截率可视化）
- 可能被深挖的问题与应对（并发/一致性/成本/幻觉/回归）

（内容按项目实际填充，避免空话；每点给可展示的证据。）

- [ ] **Step 3: 全量离线测试回归**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_regression.py tests/test_trace_to_case.py -q`
Expected: 全绿（6 + 5 = 11 passed）。

- [ ] **Step 4: 提交**

```bash
git add README.md docs/面试逐字稿.md docs/二次开发规划-生产化改造设计.md
git commit -m "docs: 生产化架构总览 + 面试逐字稿 + 收尾"
```

---

## Self-Review（作者自查）

- **Spec 覆盖**：对应设计文档第 2 节④Eval 回归 + 第 5 节 W4「回归门禁 + Trace 回流 + 部署 + 收尾」。全部覆盖。✅
- **核心零改动**：只加 evaluation/scripts/根目录文件。✅
- **纯函数可离线测**：`compare_to_baseline`、`trace_to_case`、`is_problem_trace` 均纯函数并有测试；实际评估运行沿用现有需 API 的 run_eval。✅
- **闭环完整**：线上 Trace（W2）→ 问题筛选 → 回流用例 → 评估回归门禁，串起"度量→回流→守护"。✅
- **类型一致性**：`compare_to_baseline` 消费 `Evaluator._aggregate` 产出的 summary 键名一致；`trace_to_case` 消费 `TraceStore.get_trace` 的字段一致。✅

---

## 完成即达成的里程碑

`docker compose up` 一键起服务；改坏 prompt 后 `run_eval --regression` 报错拦截；线上被拦截/转人工的对话一键回流成回归用例。至此二次开发全部完成：项目从「教学 demo」蜕变为「真实数据 + 流式服务 + 安全护栏 + 全链路可观测 + 人机协作 + 生产加固 + 评估回归 + 容器化部署」的完整生产级 Agent 系统，配套架构图与面试逐字稿。
```
