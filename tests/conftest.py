"""测试全局夹具:强制会话存储用 file 后端,套件不依赖外部 .env(如本机 .env 设了 redis)。

需要 redis 后端的测试自行 set_session_store(RedisSessionStore(fakeredis...)) 覆盖即可。

**这份夹具的职责边界**:凡是"套件的正确性依赖其取值"的 settings 字段,都必须在
这里钉成确定值。理由不是洁癖——`settings` 在导入期读 `.env`,而 `.env` 是每台
机器各自的运行配置(本机开着 aperag/redis/langfuse/demo,交付机可能全不一样)。
漏钉一个字段,表现就是"同一份代码在我这儿绿、在你那儿红",而且红的用例与真正
的改动毫无关系——排查成本远高于在这里多写一行。新增带开关的特性时,如果有测试
断言它关着(或开着)时的行为,就在这里补一行。
"""

import pytest

from app.config.settings import settings
from app.session.store import FileSessionStore, set_session_store
from app.session.lock import LocalSessionLock, set_session_lock
from app.session.idempotency import NullIdempotencyStore, set_idempotency_store


@pytest.fixture(scope="session", autouse=True)
def _isolate_trace_db(tmp_path_factory):
    """把 trace 库指向临时目录,别让测试往**真实的**观测库里写。

    实测污染程度:开发机上 `app/sessions/traces.db` 里 481 条 trace 有 175 条
    (36%)是测试造的,其中 158 条是同一句 fixture 文案「查一下订单」,延迟 0~2ms。
    后果是看板上每一个数字都掺了假——P50 被一堆 0ms 的假 trace 拉到 0,工具
    成功率混进了打桩调用的结果。而这些数字正是判断"线上现在健不健康"的依据。

    session 级而不是 function 级:trace 库是进程级单例,按用例反复换路径既没必要
    也会让跨用例的观测断言失去连续性。
    """
    orig = settings.trace_db_path
    settings.trace_db_path = str(tmp_path_factory.mktemp("traces") / "traces.db")
    yield
    settings.trace_db_path = orig


@pytest.fixture(autouse=True)
def _force_local_session_backends():
    set_session_store(FileSessionStore())        # 存储用 file
    set_session_lock(LocalSessionLock())         # 锁用进程内(不依赖 redis/env)
    set_idempotency_store(NullIdempotencyStore())  # 幂等默认关(需 redis 的测试自行注入)
    _orig_rp = settings.reply_pipeline_enabled
    settings.reply_pipeline_enabled = False   # 既有语料默认不跑流水线;需要的测试自行开启
    _orig_lf = settings.langfuse_enabled
    settings.langfuse_enabled = False         # 测试不上报 Langfuse(本机 .env 可能开着)
    _orig_auth = settings.auth_enabled
    settings.auth_enabled = False      # 既有测试不带 token;auth 专项测试自行开启
    _orig_async = settings.memory_async_updates
    settings.memory_async_updates = False  # 既有测试确定性(同步执行);异步专项测试自行开启
    _orig_kb = settings.kb_backend
    settings.kb_backend = "local"      # 召回走本地索引;ApeRAG 专项测试自行覆盖(本机 .env 可能设 aperag)
    _orig_ak = settings.aperag_api_key
    settings.aperag_api_key = ""       # 不带真实 key,防止误发外部请求
    _orig_qu = settings.query_understanding_enabled
    settings.query_understanding_enabled = False  # 既有语料走老路由;查询理解专项测试自行开启
    _orig_faq = settings.faq_cache_enabled
    settings.faq_cache_enabled = False  # 既有语料不走FAQ直答;缓存专项测试自行开启
    _orig_fb = settings.kb_local_fallback_enabled
    # 钉成 False(= 字段默认值)。本机 .env 设 KB_LOCAL_FALLBACK_ENABLED=true 时,
    # test_recall_kb.py::test_fallback_disabled_by_default_no_injection 会因为
    # "默认关"这个前提被环境改掉而失败——它测的正是关兜底时不碰本地索引。
    settings.kb_local_fallback_enabled = False
    _orig_cluster = settings.skill_semantic_clustering_enabled
    # 钉成 False:语义聚类会发 embedding 请求。既有 synth 用例断言的是关键词
    # 聚类的分组结果(它们本来就是为那套写的),语义路径由 test_skill_clustering.py
    # 打桩验证——测试里绝不真打端点。
    settings.skill_semantic_clustering_enabled = False
    _orig_pause = settings.collab_marketing_pause_open_handoffs
    # 钉成 0(=关)。这条闸会去读**真实的** hitl.db 统计未结工单数,而开发机上
    # 那张库积着几十条历史工单——闸一开,全部协作测试里的营销消费都被静默拦停,
    # 症状是"草稿数为 0",完全看不出跟工单有关。专项测试自行开启并打桩计数。
    settings.collab_marketing_pause_open_handoffs = 0
    _orig_mcp = settings.mcp_enabled
    # 钉成 False(= 字段默认值)。**本机 .env 设了 MCP_ENABLED=true**,于是每个
    # 建 Agent 的测试都会去连 `127.0.0.1:9123` 这个**独立进程**:
    #   - MCP 在跑   → 用远程 9 个工具,与 LOCAL_TOOL_DEFINITIONS 不是同一套
    #   - MCP 没跑   → MCPClient.connect() 等满 30 秒超时再降级本地
    # 症状就是"同一份代码时绿时红、套件耗时在 86s/100s/305s/615s 之间乱跳",
    # 而红的用例与真正的改动毫无关系。实测 test_orchestrator_stream_eligible.py
    # 的 3 条就这样失败过一次、单独跑却全过。
    #
    # 三个文件(test_replay_e2e / test_warmup / test_eval_sandbox_settings_restore)
    # 此前已各自防御性地钉过这个值——那说明坑被踩过,但补在了局部而不是源头。
    # MCP 集成测试走 RUN_MCP_INTEGRATION 显式开关 + 自建 ToolManager,不受这里影响。
    settings.mcp_enabled = False
    _orig_demo = settings.demo_mode
    # 钉成 False。开着时 /api/chat 会给 demo 用户注入 hmdp 身份并改写 user_id,
    # 会话归属类断言(reset/翻篇/历史回显)会连带失真。
    settings.demo_mode = False
    yield
    settings.kb_local_fallback_enabled = _orig_fb
    settings.mcp_enabled = _orig_mcp
    settings.demo_mode = _orig_demo
    settings.collab_marketing_pause_open_handoffs = _orig_pause
    settings.skill_semantic_clustering_enabled = _orig_cluster
    settings.reply_pipeline_enabled = _orig_rp
    settings.langfuse_enabled = _orig_lf
    settings.auth_enabled = _orig_auth
    settings.memory_async_updates = _orig_async
    settings.kb_backend = _orig_kb
    settings.aperag_api_key = _orig_ak
    settings.query_understanding_enabled = _orig_qu
    settings.faq_cache_enabled = _orig_faq
    set_session_store(None)
    set_session_lock(None)
    set_idempotency_store(None)
