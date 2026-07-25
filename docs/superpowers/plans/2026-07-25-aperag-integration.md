# ApeRAG 接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把统一召回层的 KB 源升级为可切换后端——接入用户参与的开源 RAG 平台 ApeRAG(向量+全文混合检索+可选重排),三级降级(ApeRAG→本地索引→无注入),并补上多轮查询改写(指代/省略消解),同时保持"全定义/选择性启用"的容器部署形态。

**Architecture:** ApeRAG 以 docker compose 部署在 `D:\2026项目\ApeRAG`(核心 9 容器默认启动,neo4j/docray/jaeger 挂 profile 按需);ecom 项目侧新增 `external_kb.py` 适配器调用其 `POST /api/v1/collections/{id}/searches`,`kb.py` 按 `settings.kb_backend` 分发并降级;`rewrite.py` 在预召回前用 LLM 把口语化追问改写为自包含查询,失败回退原句。存储红线不变:ApeRAG 是独立服务,不与用户记忆混存。

**Tech Stack:** Docker Compose(profiles)、ApeRAG API(Bearer 认证)、httpx 0.28(项目已有)、pytest、React/vitest(前端小改)。

## Global Constraints

- Python 一律 `.venv/Scripts/python.exe`(ecom 项目 venv);pytest:`.venv/Scripts/python.exe -m pytest <files> -q`
- Windows gbk 控制台:脚本/测试不得 print emoji;中文可以
- 提交信息末尾:`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;只在 ecom 仓库提交;只推 `origin feature/w1-service-streaming`(本计划不推送);`.env` 严禁提交(APERAG_API_KEY 放 .env)
- **ApeRAG 仓库(D:\2026项目\ApeRAG)只允许新增 `docker-compose.override.yml` 和 `.env`(均不提交),不得改动其任何受版本控制的文件**
- 容错红线:external 后端任何失败(连接拒/超时/非200/解析错)返回 None 触发降级,绝不抛出;查询改写任何失败返回原句
- 热路径延迟预算:external 检索复用 `recall_kb_timeout_s=6.0`;改写 LLM 调用同样受该超时约束
- 全量测试基线:分支预存 live 集成测试失败(test_agent/test_memory/test_rag 等),回归以"无新增失败"为准,验证只跑指定文件
- 端口约定:ApeRAG api 映射 **8100**、frontend 映射 **3100**(3000 被 Langfuse 占用)
- 现有接口不得破坏:`kb_recall(query) -> KbRecall`、`KbRecall(section, hits)` 的形状不变(新增 `backend` 字段带默认值允许)

---

### Task 1: ApeRAG 部署(全定义/选择性启用)

**Files:**
- Create: `D:\2026项目\ApeRAG\docker-compose.override.yml`(不提交,ApeRAG 仓库本地文件)
- Create: `D:\2026项目\ApeRAG\.env`(从 envs/env.template 复制,不提交)
- Test: 无 pytest;验收 = 命令输出断言(本任务是 ops,按步骤验证)

**Interfaces:**
- Produces: ApeRAG API 就绪于 `http://127.0.0.1:8100`(`/docs` 返回 200),Web 界面于 `http://127.0.0.1:3100/web/`。Task 2 的手册与冒烟依赖这两个地址。

- [ ] **Step 1: 腾内存(机器 16G,当前可用 <1G)**

```bash
docker stop dify-db-1 dify-redis-1
docker ps --format "{{.Names}}" | head -20
```

Expected: dify 两容器消失于列表;Langfuse 6 容器与 ecom-redis 保留。
(注:`docker-db-1`/`docker-redis-1` 归属不明,先不动;若后续内存仍紧张再问用户。)

- [ ] **Step 2: 写端口重映射 + flower 降级 override**

创建 `D:\2026项目\ApeRAG\docker-compose.override.yml`:

```yaml
# 本地覆写(不提交):端口避让 + 非必需服务挂 profile
# - 3000 被 Langfuse 占用 → frontend 3100;api 统一走 8100
# - flower(celery 监控面板)平时不需要 → 挪进 monitoring profile,要看时 --profile monitoring
services:
  api:
    ports:
      - "8100:8000"
  frontend:
    ports:
      - "3100:3000"
  flower:
    profiles: ["monitoring"]
```

- [ ] **Step 3: 准备 .env**

```bash
cd "D:/2026项目/ApeRAG" && cp envs/env.template .env
```

- [ ] **Step 4: 全量拉取镜像(满足"全部都在",只花磁盘)**

```bash
cd "D:/2026项目/ApeRAG" && docker compose --profile neo4j --profile docray --profile jaeger --profile monitoring pull
```

Expected: 所有镜像 pull 完成(docray 与 docray-gpu 同镜像,只下一份)。首次约 5-10GB,耐心等。

- [ ] **Step 5: 启动核心(默认 profile,9 服务)**

```bash
cd "D:/2026项目/ApeRAG" && docker compose up -d
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

Expected: api/frontend/celeryworker/celerybeat/postgres/redis/qdrant/es 为 Up(flower 因 override 不启动;neo4j/docray/jaeger 不启动)。es 启动慢,等 1-2 分钟。

- [ ] **Step 6: 就绪验证**

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8100/docs
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3100/web/
```

Expected: 两个都是 `200`(api 首次迁移数据库可能要再等 ~1 分钟,重试即可)。

- [ ] **Step 7: 记录按需启用命令(写入 Task 2 的手册,此处先验证语法)**

```bash
cd "D:/2026项目/ApeRAG" && docker compose --profile neo4j config --services | grep neo4j
```

Expected: 输出 `neo4j`(证明 profile 可用;不真启动)。

无 git 提交(本任务不触碰 ecom 仓库;override/.env 在 ApeRAG 仓库且为本地未跟踪文件)。

---

### Task 2: 语料初始化手册 + 检索冒烟脚本

**Files:**
- Create: `docs/ApeRAG接入-操作手册.md`(ecom 仓库)
- Create: `scripts/aperag_smoke.py`(ecom 仓库)
- Modify: `D:\2026项目\ecom-service-agent\.env`(追加 APERAG_* 四项;不提交)
- Test: 冒烟脚本实跑(依赖用户完成 UI 步骤后)

**Interfaces:**
- Consumes: Task 1 的 `http://127.0.0.1:8100`
- Produces: `.env` 中 `KB_BACKEND/APERAG_BASE_URL/APERAG_API_KEY/APERAG_COLLECTION_ID` 可用;Task 3 的适配器按这些配置调用。
- **注意:本任务有用户协作点**——注册账号、配置模型提供商(涉及账号创建与 API Key 填入,必须用户亲自在界面操作)。代理只负责:写手册、写脚本、用户完成后跑冒烟验收。

- [ ] **Step 1: 写操作手册**

创建 `docs/ApeRAG接入-操作手册.md`:

```markdown
# ApeRAG 接入操作手册(一次性初始化)

部署形态:全定义/选择性启用——镜像全在本地,默认只跑核心 9 容器。

## 服务地址
- Web 界面: http://127.0.0.1:3100/web/
- API 文档: http://127.0.0.1:8100/docs

## 按需启用/关闭重量级组件
    cd D:\2026项目\ApeRAG
    docker compose --profile neo4j up -d       # 开 GraphRAG(演示前建议先 stop Langfuse 栈腾内存)
    docker compose --profile neo4j stop neo4j  # 用完关
    docker compose --profile monitoring up -d  # 开 flower(celery 监控)
    docker compose --profile docray up -d      # 开重型文档解析(8G 预留,md 语料用不到)

## 一次性初始化(用户在浏览器操作)
1. 打开 http://127.0.0.1:3100/web/ ,注册账号(首个账号即管理员)并登录
2. 模型提供商:设置 → 模型服务商 → 添加 OpenAI 兼容提供商,
   Base URL 与 API Key 填 ecom 项目 .env 里的 OPENAI_BASE_URL / OPENAI_API_KEY;
   确认 embedding 模型(text-embedding-3-small 或提供商等价物)与一个对话模型可用,
   并在"默认模型"里把 embedding 默认项设置好
3. 新建知识库(collection):名称「并夕夕客服知识库」,
   索引开关:向量 ✅ 全文 ✅ 知识图谱 ❌(neo4j 未启动) 摘要 ❌ 视觉 ❌
4. 上传 4 篇文档:D:\2026项目\ecom-service-agent\app\agent\rag\knowledge\ 下的
   退换货政策.md / 配送说明.md / 会员权益.md / 常见问题FAQ.md,等待索引状态全部完成
5. 生成 API Key:设置 → API Keys → 创建,复制
6. 取 collection id:知识库详情页 URL 中 col_ 开头的段
7. 把以下四行追加到 ecom 项目 .env(值换成实际):
       KB_BACKEND=aperag
       APERAG_BASE_URL=http://127.0.0.1:8100
       APERAG_API_KEY=sk-xxxx
       APERAG_COLLECTION_ID=col_xxxx

## 验收
    cd D:\2026项目\ecom-service-agent
    .venv\Scripts\python.exe scripts\aperag_smoke.py "七天无理由退货怎么退"
预期:HTTP 200,命中若干条,来源含「退换货政策」,recall_type 含 vector_search 或 fulltext_search。

## 回滚
.env 里 KB_BACKEND=local 即回到项目内本地索引(或删除该行,默认就是 local)。
```

- [ ] **Step 2: 写冒烟脚本**

创建 `scripts/aperag_smoke.py`:

```python
"""ApeRAG 检索冒烟:验证 collection 就绪、混合检索能命中政策文档。

用法: .venv/Scripts/python.exe scripts/aperag_smoke.py [查询]
依赖 .env 的 APERAG_BASE_URL / APERAG_API_KEY / APERAG_COLLECTION_ID。
"""

import sys

import httpx

from app.config.settings import settings


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "七天无理由退货怎么退"
    if not settings.aperag_api_key or not settings.aperag_collection_id:
        print("缺少 APERAG_API_KEY / APERAG_COLLECTION_ID,先按手册完成初始化")
        return 1
    url = (f"{settings.aperag_base_url.rstrip('/')}/api/v1/collections/"
           f"{settings.aperag_collection_id}/searches")
    resp = httpx.post(
        url,
        json={"query": query,
              "vector_search": {"topk": 3},
              "fulltext_search": {"topk": 3},
              "rerank": False},
        headers={"Authorization": f"Bearer {settings.aperag_api_key}"},
        timeout=30,
    )
    print("HTTP", resp.status_code)
    if resp.status_code != 200:
        print(resp.text[:300])
        return 1
    items = resp.json().get("items") or []
    print(f"命中 {len(items)} 条:")
    for it in items[:5]:
        src = str(it.get("source") or "")
        print(f"[{it.get('recall_type')}] score={float(it.get('score') or 0):.3f} src={src}")
        print("   ", (it.get("content") or "")[:80].replace("\n", " "))
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

(注意:Task 3 会给 Settings 增加 aperag_* 字段——本脚本在 Task 3 合并前运行会报 AttributeError,属预期顺序;若需先跑,临时用环境变量+os.getenv 版本验证亦可,但**最终提交的必须是上面这份读 settings 的版本**。执行顺序上建议:本任务先提交手册+脚本,用户做 UI 初始化的同时 Task 3 并行推进,Task 3 合并后再跑验收。)

- [ ] **Step 3: 提交(手册+脚本)**

```bash
cd "D:/2026项目/ecom-service-agent"
git add docs/ApeRAG接入-操作手册.md scripts/aperag_smoke.py
git commit -m "docs: ApeRAG 接入操作手册 + 检索冒烟脚本"
```

- [ ] **Step 4: 用户协作点(阻塞后续验收,不阻塞 Task 3 开发)**

提示用户按手册第 1-7 步完成 UI 初始化;完成后执行:

```bash
cd "D:/2026项目/ecom-service-agent" && ".venv/Scripts/python.exe" scripts/aperag_smoke.py "七天无理由退货怎么退"
```

Expected: `HTTP 200`,命中 ≥1 条,src 含 `退换货政策`。

---

### Task 3: external 后端适配器 + 三级降级

**Files:**
- Create: `app/agent/recall/external_kb.py`
- Modify: `app/agent/recall/kb.py`(抽取本地行获取 + 按 backend 分发)
- Modify: `app/config/settings.py`(kb_backend + aperag_* 五项,插在 recall_kb_embed_retries 之后)
- Test: `tests/test_external_kb.py`(新)+ `tests/test_recall_kb.py`(追加分发用例)

**Interfaces:**
- Consumes: ApeRAG `POST {base}/api/v1/collections/{cid}/searches`,请求 `{"query", "vector_search": {"topk"}, "fulltext_search": {"topk"}, "rerank"}`,响应 `{"items": [{"rank","score","content","source","recall_type","metadata"}]}`;Bearer 认证
- Produces: `aperag_search(query: str) -> list[dict] | None` —— None=服务不可用(触发降级),`[]`=服务正常但无命中(不降级);行形状与本地一致 `{"doc","section","score","text"}`。`KbRecall` 新增字段 `backend: str = "local"`。Task 4/5 不依赖新字段以外的变化。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_external_kb.py`:

```python
"""ApeRAG 适配器:映射/容错/未配置短路。"""

import httpx

import app.agent.recall.external_kb as ext
from app.agent.recall.external_kb import aperag_search
from app.config.settings import settings


def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "aperag_api_key", "sk-test")
    monkeypatch.setattr(settings, "aperag_collection_id", "col_test")


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_maps_items_to_standard_rows(monkeypatch):
    _cfg(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp(200, {"items": [
            {"rank": 1, "score": 0.87, "content": "签收7天内可退",
             "source": "docs/退换货政策.md", "recall_type": "vector_search"},
        ]})

    monkeypatch.setattr(ext.httpx, "post", fake_post)
    rows = aperag_search("退货政策")
    assert rows == [{"doc": "退换货政策.md", "section": "vector_search",
                     "score": 0.87, "text": "签收7天内可退"}]
    assert "col_test/searches" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["rerank"] is False
    assert captured["timeout"] == settings.recall_kb_timeout_s


def test_empty_items_is_no_hit_not_degrade(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(ext.httpx, "post",
                        lambda *a, **k: _Resp(200, {"items": []}))
    assert aperag_search("无关问题") == []          # [] 表示正常无命中


def test_http_error_returns_none(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(ext.httpx, "post",
                        lambda *a, **k: _Resp(500, text="boom"))
    assert aperag_search("退货政策") is None        # None 触发降级


def test_network_exception_returns_none(monkeypatch):
    _cfg(monkeypatch)

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ext.httpx, "post", boom)
    assert aperag_search("退货政策") is None


def test_missing_config_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "aperag_api_key", "")
    called = []
    monkeypatch.setattr(ext.httpx, "post", lambda *a, **k: called.append(1))
    assert aperag_search("退货政策") is None
    assert called == []                              # 未配置连请求都不发
```

在 `tests/test_recall_kb.py` 末尾追加分发用例:

```python
def test_backend_dispatch_aperag_used_when_available(monkeypatch):
    import app.agent.recall.kb as kb_mod2
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search",
                        lambda q: [{"doc": "退换货政策.md", "section": "vector_search",
                                    "score": 0.9, "text": "外部命中"}])
    local_called = []
    monkeypatch.setattr(kb_mod2, "search_knowledge",
                        lambda q, top_k: local_called.append(q))
    r = kb_recall("退货政策是什么")
    assert "外部命中" in r.section and r.backend == "aperag"
    assert local_called == []                        # 外部可用则不碰本地


def test_backend_dispatch_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", lambda q: None)
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天", 0.8, "本地兜底命中")))
    r = kb_recall("退货政策是什么")
    assert "本地兜底命中" in r.section and r.backend == "local"


def test_backend_default_local_untouched(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天", 0.8, "本地命中")))
    r = kb_recall("退货政策是什么")
    assert "本地命中" in r.section and r.backend == "local"


def test_aperag_rows_skip_min_score_but_respect_budget(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    long_text = "长" * 1200
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search",
                        lambda q: [{"doc": "a.md", "section": "fulltext_search",
                                    "score": 0.05, "text": "低分但服务端已排序"},
                                   {"doc": "b.md", "section": "vector_search",
                                    "score": 0.9, "text": long_text}])
    r = kb_recall("退货政策是什么")
    assert "低分但服务端已排序" in r.section          # 外部行不做 min_score 过滤
    assert long_text not in r.section                # 预算仍生效
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_external_kb.py tests/test_recall_kb.py -q`
Expected: FAIL(`No module named 'app.agent.recall.external_kb'`;分发用例 AttributeError kb_backend)

- [ ] **Step 3: 加配置**

`app/config/settings.py` 在 `recall_kb_embed_retries` 行之后插入:

```python
    # ApeRAG 外部 RAG 接入(kb_backend=aperag 时生效;三级降级 aperag→local→无注入)
    kb_backend: str = "local"          # local=项目内向量索引; aperag=外部 ApeRAG(向量+全文混合)
    aperag_base_url: str = "http://127.0.0.1:8100"
    aperag_api_key: str = ""           # ApeRAG 控制台创建(Bearer);放 .env 勿提交
    aperag_collection_id: str = ""     # 知识库 collection id(col_ 开头)
    aperag_rerank: bool = False        # 预召回热路径默认关重排(省延迟);深查精度可开
```

- [ ] **Step 4: 写适配器**

创建 `app/agent/recall/external_kb.py`:

```python
"""ApeRAG 外部检索源:统一召回层 KB 源的 external 后端。

调用本机 ApeRAG(用户参与的开源 RAG 平台)collection searches API——
向量+全文双路混合检索,服务端融合排序,可选重排。返回与本地后端相同的
标准化行,由 kb.py 统一做预算/格式化。

约定:None=服务不可用(kb.py 降级本地索引);[]=服务正常但无命中(不降级)。
容错红线:任何失败(连接拒/超时/非200/解析错)都只 warning + 返回 None。
"""

import logging

import httpx

from app.config.settings import settings

logger = logging.getLogger(__name__)


def aperag_search(query: str) -> list[dict] | None:
    if not settings.aperag_api_key or not settings.aperag_collection_id:
        logger.warning("kb_backend=aperag 但缺少 api_key/collection_id,降级本地")
        return None
    url = (f"{settings.aperag_base_url.rstrip('/')}/api/v1/collections/"
           f"{settings.aperag_collection_id}/searches")
    payload = {
        "query": query,
        "vector_search": {"topk": settings.recall_kb_top_k},
        "fulltext_search": {"topk": settings.recall_kb_top_k},
        "rerank": settings.aperag_rerank,
    }
    try:
        resp = httpx.post(url, json=payload,
                          headers={"Authorization": f"Bearer {settings.aperag_api_key}"},
                          timeout=settings.recall_kb_timeout_s)
        if resp.status_code != 200:
            logger.warning("aperag search http %s: %s", resp.status_code, resp.text[:200])
            return None
        items = resp.json().get("items") or []
    except Exception:
        logger.warning("aperag search failed", exc_info=True)
        return None
    rows = []
    for it in items:
        src = str(it.get("source") or "")
        rows.append({
            "doc": src.replace("\\", "/").rsplit("/", 1)[-1] or "知识库",
            "section": str(it.get("recall_type") or ""),
            "score": float(it.get("score") or 0.0),
            "text": str(it.get("content") or ""),
        })
    return rows
```

- [ ] **Step 5: 改 kb.py 分发**

`app/agent/recall/kb.py` 改动三处(保持既有行为不变):

5a. `KbRecall` 增加字段(带默认,向后兼容):

```python
@dataclass
class KbRecall:
    section: str | None = None                       # 可注入的 system prompt 段;无命中为 None
    hits: list[dict] = field(default_factory=list)   # [{"doc","section","score"}] 供事件/观测展示
    backend: str = "local"                           # 本轮实际使用的后端(local/aperag),供观测
```

5b. 在 `kb_recall` 之前新增两个取行函数(把现有 search_knowledge 调用与 success 判断逻辑**移**进 `_local_rows`,含现有的 warning 日志):

```python
def _local_rows(query: str) -> list[dict]:
    """本地向量索引取行;失败返回 []。"""
    try:
        result = search_knowledge(query, top_k=settings.recall_kb_top_k)
    except Exception:
        logger.warning("kb pre-recall search failed", exc_info=True)
        return []
    if not result.get("success"):
        logger.warning("kb pre-recall degraded: %s", result.get("error"))
        return []
    return result.get("results", [])


def _fetch_rows(query: str) -> tuple[list[dict], str]:
    """按 kb_backend 取行,三级降级:aperag→local→[](调用方无命中即不注入)。"""
    if settings.kb_backend == "aperag":
        from app.agent.recall.external_kb import aperag_search
        rows = aperag_search(query)
        if rows is not None:
            return rows, "aperag"
        logger.warning("kb backend aperag unavailable, fallback to local index")
    return _local_rows(query), "local"
```

5c. `kb_recall` 主体改为消费 `_fetch_rows`(门控/预算/格式化保持不变;**min_score 只对 local 行生效**——aperag 行已由服务端融合排序,分数刻度不同):

```python
def kb_recall(query: str | None) -> KbRecall:
    """对本轮用户问题做 KB 预检索,返回格式化注入段与命中明细。"""
    if not settings.recall_kb_enabled:
        return KbRecall()
    if not query or len(query.strip()) < settings.recall_kb_min_query_chars:
        return KbRecall()
    rows, backend = _fetch_rows(query)

    lines: list[str] = []
    hits: list[dict] = []
    used = len(_HEADER)
    for r in rows:
        if backend == "local" and r.get("score", 0.0) < settings.recall_kb_min_score:
            continue
        line = f"- [{r.get('doc', '')}/{r.get('section', '')}] {r.get('text', '')}"
        if used + len(line) > settings.recall_kb_max_chars:
            break
        lines.append(line)
        used += len(line)
        hits.append({"doc": r.get("doc", ""), "section": r.get("section", ""),
                     "score": r.get("score", 0.0)})
    if not lines:
        return KbRecall(backend=backend)
    return KbRecall(section=_HEADER + "\n" + "\n".join(lines), hits=hits, backend=backend)
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_external_kb.py tests/test_recall_kb.py tests/test_recall_service.py tests/test_recall_wiring.py -q`
Expected: 全部通过(新 5+4 用例 + 既有回归)

- [ ] **Step 7: 提交**

```bash
cd "D:/2026项目/ecom-service-agent"
git add app/agent/recall/external_kb.py app/agent/recall/kb.py app/config/settings.py tests/test_external_kb.py tests/test_recall_kb.py
git commit -m "feat: KB 源接入 ApeRAG external 后端(混合检索,三级降级 aperag→local→无注入)"
```

---

### Task 4: 多轮查询改写

**Files:**
- Create: `app/agent/recall/rewrite.py`
- Modify: `app/config/settings.py`(2 项,插在 aperag_rerank 之后)
- Modify: `app/agent/chat.py`(`_build_messages` 缓存未命中分支:改写后再召回,事件带 query)
- Modify: `webui/src/components/AgentActivity.tsx`(recall 行显示改写查询)
- Test: `tests/test_recall_rewrite.py`(新)+ `tests/test_recall_wiring.py`(追加)+ `webui/src/tests/chat-components.test.tsx`(改 recall 用例)

**Interfaces:**
- Consumes: `EcomAgent.client`(OpenAI 兼容,可能是 resilient 代理)、`EcomAgent.raw_messages`
- Produces: `rewrite_for_recall(client, model: str, messages: list[dict], query: str | None) -> str | None`;recall 事件新增字段 `"query": <实际用于检索的查询>`(向后兼容,前端无该字段时照旧渲染)

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recall_rewrite.py`:

```python
"""查询改写:多轮补全指代;首问/关闭/失败/空输出一律回退原句。"""

from types import SimpleNamespace

from app.agent.recall.rewrite import rewrite_for_recall
from app.config.settings import settings


def _client(reply=None, raises=False, capture=None):
    def create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if raises:
            raise RuntimeError("llm down")
        msg = SimpleNamespace(content=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _msgs(*users):
    out = []
    for u in users:
        out.append({"role": "user", "content": u})
        out.append({"role": "assistant", "content": "好的"})
    return out


def test_multi_turn_rewrites(monkeypatch):
    cap = {}
    c = _client(reply="退货运费谁承担", capture=cap)
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么", "那运费呢?"), "那运费呢?")
    assert got == "退货运费谁承担"
    sent = str(cap["messages"])
    assert "退货政策是什么" in sent and "那运费呢?" in sent   # 历史与原句都送给了改写


def test_first_turn_skips_llm():
    c = _client(raises=True)                                  # 若真调 LLM 会抛
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么"), "退货政策是什么")
    assert got == "退货政策是什么"


def test_disabled_returns_raw(monkeypatch):
    monkeypatch.setattr(settings, "recall_rewrite_enabled", False)
    c = _client(raises=True)
    got = rewrite_for_recall(c, "m", _msgs("a", "b"), "b")
    assert got == "b"


def test_llm_error_returns_raw():
    c = _client(raises=True)
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么", "那运费呢?"), "那运费呢?")
    assert got == "那运费呢?"


def test_empty_output_returns_raw():
    c = _client(reply="   ")
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么", "那运费呢?"), "那运费呢?")
    assert got == "那运费呢?"


def test_none_query_passthrough():
    c = _client(raises=True)
    assert rewrite_for_recall(c, "m", [], None) is None
```

在 `tests/test_recall_wiring.py` 追加:

```python
def test_recall_uses_rewritten_query_and_event_carries_it(monkeypatch):
    calls = []

    def fake_recall(mm, query):
        calls.append(query)
        return RecallResult(
            sections=[{"role": "system", "content": "【平台知识(自动检索)】X"}],
            kb_hits=[{"doc": "d", "section": "s", "score": 0.9}],
        )

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    monkeypatch.setattr("app.agent.recall.rewrite.rewrite_for_recall",
                        lambda client, model, messages, q: "改写后的自包含查询")
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "那运费呢?"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == ["改写后的自包含查询"]            # 召回吃的是改写后查询
    ev = [e for e in events if e["type"] == "recall"][0]
    assert ev["query"] == "改写后的自包含查询"        # 事件带上实际检索查询
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_rewrite.py tests/test_recall_wiring.py -q`
Expected: FAIL(`No module named 'app.agent.recall.rewrite'`;wiring 新用例 KeyError 'query')

- [ ] **Step 3: 加配置**

`app/config/settings.py` 在 `aperag_rerank` 行之后插入:

```python
    # 多轮查询改写(预召回前置):"那运费呢?"→"退货运费谁承担"。失败/首问回退原句
    recall_rewrite_enabled: bool = True
    recall_rewrite_max_turns: int = 6  # 改写时参考的最近用户消息条数
```

- [ ] **Step 4: 写改写器**

创建 `app/agent/recall/rewrite.py`:

```python
"""多轮查询改写:把口语化/指代式的最新消息改写为自包含检索查询。

预召回按用户原句做检索,多轮追问("那运费呢?""第二种呢?")的指代和省略
会让向量/全文检索双双失准——这是客服对话的常态,生产 RAG 的标准前置步。

容错红线:LLM 失败/超时/空输出/首问无上下文,一律返回原句,绝不阻塞主流程。
超时复用 recall_kb_timeout_s(热路径预算);resilient 代理不支持 per-request
timeout 参数时自动降级为无 timeout 参数调用(仍有代理自身的超时兜底)。
"""

import logging

from app.config.settings import settings

logger = logging.getLogger(__name__)

_PROMPT = (
    "把用户最新消息改写成一条自包含的知识库检索查询:结合最近对话补全其中的"
    "指代与省略;如果已经自包含,原样返回。只输出改写后的查询,不要任何解释。"
)


def rewrite_for_recall(client, model: str, messages: list[dict],
                       query: str | None) -> str | None:
    if not settings.recall_rewrite_enabled or not query:
        return query
    users = [m.get("content", "") for m in messages if m.get("role") == "user"]
    if len(users) < 2:
        return query          # 首问没有可补全的上下文,省一次 LLM
    recent = users[-settings.recall_rewrite_max_turns:]
    context = "\n".join(f"- {u}" for u in recent[:-1])
    req = dict(
        model=model, temperature=0.0, max_tokens=80,
        messages=[{"role": "system", "content": _PROMPT},
                  {"role": "user", "content": f"最近对话:\n{context}\n\n用户最新消息:{query}"}],
    )
    try:
        try:
            resp = client.chat.completions.create(
                timeout=settings.recall_kb_timeout_s, **req)
        except TypeError:      # 代理不接受 per-request timeout
            resp = client.chat.completions.create(**req)
        text = (resp.choices[0].message.content or "").strip()
        return text or query
    except Exception:
        logger.warning("recall query rewrite failed, use raw query", exc_info=True)
        return query
```

- [ ] **Step 5: 接线 chat.py**

`app/agent/chat.py` `_build_messages` 中,把缓存未命中分支:

```python
        if self._turn_recall is None or self._turn_recall[0] != last_user:
            rr = build_recall_sections(self.memory_manager, last_user)
            self._turn_recall = (last_user, rr)
            if rr.kb_hits:   # 首次计算且 KB 有命中才发事件(前端思考面板+tracer 各消费一次)
                self._emit({"type": "recall", "source": "kb", "hits": rr.kb_hits})
```

替换为:

```python
        if self._turn_recall is None or self._turn_recall[0] != last_user:
            # 先改写(多轮指代/省略消解),再统一召回;缓存键仍是 last_user(轮身份)
            from app.agent.recall.rewrite import rewrite_for_recall
            recall_query = rewrite_for_recall(self.client, self.model,
                                              self.raw_messages, last_user)
            rr = build_recall_sections(self.memory_manager, recall_query)
            self._turn_recall = (last_user, rr)
            if rr.kb_hits:   # 首次计算且 KB 有命中才发事件(前端思考面板+tracer 各消费一次)
                self._emit({"type": "recall", "source": "kb",
                            "query": recall_query, "hits": rr.kb_hits})
```

- [ ] **Step 6: 前端显示改写查询**

`webui/src/components/AgentActivity.tsx` 的 recall 行:

```tsx
              {e.type === "recall" && <><BookOpen className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>预召回{e.query ? `（查询：${e.query}）` : ""} → 平台知识：{(e.hits as { doc: string; section: string }[] ?? []).map(h => `${h.doc}/${h.section}`).join("、")}</span></>}
```

`webui/src/tests/chat-components.test.tsx` 的 recall 用例事件对象加 `query` 字段并断言:

```tsx
      { type: "recall", source: "kb", query: "退货运费谁承担", hits: [
        { doc: "退换货政策", section: "七天无理由", score: 0.62 },
        { doc: "会员权益", section: "钻石会员", score: 0.41 },
      ] },
```

追加断言:`expect(screen.getByText(/退货运费谁承担/)).toBeInTheDocument();`

- [ ] **Step 7: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_recall_rewrite.py tests/test_recall_wiring.py tests/test_recall_kb.py tests/test_external_kb.py tests/test_recall_service.py -q`
Expected: 全部通过

Run: `cd webui && npx vitest run && npx tsc --noEmit`
Expected: 全部通过,tsc 无输出

- [ ] **Step 8: 提交**

```bash
cd "D:/2026项目/ecom-service-agent"
git add app/agent/recall/rewrite.py app/config/settings.py app/agent/chat.py webui/src/components/AgentActivity.tsx webui/src/tests/chat-components.test.tsx tests/test_recall_rewrite.py tests/test_recall_wiring.py
git commit -m "feat: 多轮查询改写(指代/省略消解)接入预召回,事件带实际检索查询"
```

---

### Task 5: 端到端验收 + 文档更新

**Files:**
- Modify: `docs/统一召回层-作用与效果.md`(追加 ApeRAG 接入与查询改写章节)
- Test: 端到端冒烟(依赖 Task 2 用户协作点完成;若未完成,记录为待验收项并验证 local 降级路径)

**Interfaces:**
- Consumes: 全部前序任务

- [ ] **Step 1: 端到端冒烟(aperag 后端)**

前置:用户已完成手册初始化,`.env` 已配 KB_BACKEND=aperag 等四项。

```bash
cd "D:/2026项目/ecom-service-agent" && ".venv/Scripts/python.exe" -X utf8 -c "
from app.agent.recall.kb import kb_recall
r = kb_recall('七天无理由退货怎么退')
print('backend:', r.backend)
print('hits:', [(h['doc'], h['section']) for h in r.hits])
print('section head:', (r.section or '')[:120])
"
```

Expected: `backend: aperag`,hits 来源含 `退换货政策.md`,recall_type 为 vector_search/fulltext_search。

- [ ] **Step 2: 降级链验证(停 ApeRAG api 再跑)**

```bash
cd "D:/2026项目/ApeRAG" && docker compose stop api
cd "D:/2026项目/ecom-service-agent" && ".venv/Scripts/python.exe" -X utf8 -c "
from app.agent.recall.kb import kb_recall
r = kb_recall('七天无理由退货怎么退')
print('backend:', r.backend, '| has section:', bool(r.section))
"
cd "D:/2026项目/ApeRAG" && docker compose start api
```

Expected: `backend: local | has section: True`(6 秒内降级,不挂死)。

- [ ] **Step 3: 追加文档章节**

在 `docs/统一召回层-作用与效果.md` 末尾追加:

```markdown
## 升级:ApeRAG 外部后端 + 多轮查询改写

### ApeRAG 接入(kb_backend=aperag)

KB 源支持双后端:`local`(项目内向量索引)/ `aperag`(外部 ApeRAG 平台,
向量+全文双路混合检索、服务端融合、可选重排)。ApeRAG 为本人参与的开源
RAG 项目,部署形态为"全定义/选择性启用":核心 9 容器常驻,GraphRAG(neo4j)/
重型文档解析(docray)/链路追踪(jaeger)挂 compose profile 按需启停。

三级降级链:ApeRAG(6s 超时)→ 本地索引 → 无注入段——外部服务任何故障
都不影响回复主流程,与既有容错红线一致。初始化与启停命令见
《ApeRAG接入-操作手册》。

### 多轮查询改写(recall_rewrite_enabled)

预召回原本拿用户原句做检索,多轮追问("那运费呢?")的指代/省略会让
命中率骤降。现在预召回前先经一次轻量 LLM 改写为自包含查询
("退货运费谁承担"),首问/失败/超时一律回退原句。改写后的实际检索
查询随 recall 事件透出,前端思考面板与 tracer 均可见。

### 检索链路全景

    用户消息 → 查询改写(多轮消解) → 统一召回
      ├─ profile / 长期记忆 / 短期摘要(本地,一人一档)
      └─ KB:ApeRAG(混合检索+重排) ↓降级 本地向量索引 ↓降级 无注入
    → 注入上下文 → ReAct → 回复流水线(评估器兜底)
```

- [ ] **Step 4: 提交**

```bash
cd "D:/2026项目/ecom-service-agent"
git add docs/统一召回层-作用与效果.md
git commit -m "docs: ApeRAG 接入与多轮查询改写章节"
```
