"""进程启动预热:把原本摊在"第一个真实用户"身上的处级构造成本提前搬到启动阶段。

问题背景(W1 服务化 L2 测量):新会话构造在冷启动的进程上首次约 4~6s，其中
绝大部分是 `ToolManager` 首次建 MCP 共享连接(`app/mcp_client/shared.py`)；
第二个及之后的新会话约 0.86s，用 cProfile 定位后发现 98% 以上耗在每个新
`OpenAI`/`Embedder` 客户端各自建一次的 SSL 上下文(见
`app/observability/http_pool.py`)。两者都是**进程级**成本，与具体哪个会话/
哪个用户无关，理应在服务启动时一次性付掉，而不是让"第一个真实用户"买单。

预热覆盖:
  - MCP 工具连接(进程级共享单例，get_shared_mcp_client)
  - LLM/Embedding 共用的 httpx 传输层(进程级共享池，get_shared_http_client)
  - FAQ 语义缓存(本地 JSON 文件加载)
  - 知识库本地向量索引 + embedder 客户端构造(只碰本地文件/建客户端，
    不发真实网络请求，也绝不触达外部 ApeRAG——见 warm_local_retriever 说明)

铁律(该项目已经被"后台线程悄悄没跑/跑挂了没人知道"咬过不止一次，
见 SessionManager 的 reaper 与本次任务描述里的 5.94s 冷启动本身):
  1) 预热必须在后台线程跑，绝不阻塞 create_app() 返回——服务必须先能接客，
     预热是锦上添花，不是前置条件。
  2) 预热失败 = 这一项优化没生效，不是服务不可用——任何异常都不能往外抛，
     首个真实请求照付它今天已经在付的成本，不能因为预热本身出错反而
     让服务整体起不来。
  3) 预热耗时与成败必须发可观测信号(至少一条 print + logger，两条都发是
     为了不管有没有接日志系统都能在控制台看到)，不能悄悄跑完/悄悄失败。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


def _warm_mcp() -> None:
    """MCP 工具连接:进程级共享单例，与 ToolManager._init_mcp 走的是同一份缓存。"""
    from app.config.settings import settings
    if not settings.mcp_enabled:
        return
    from app.mcp_client import get_shared_mcp_client
    shared = get_shared_mcp_client(settings.mcp_server_url)
    if shared is None:
        # get_shared_mcp_client 本身已经是 fail-soft(失败返回 None)；这里
        # 显式转成异常，让 warm_process() 的计时/成败统计如实记一条"失败"，
        # 而不是被当成"什么都没做的成功"悄悄放过——ToolManager 会在真正
        # 需要时自动重试并降级本地工具，预热这里只是没能替它把这份成本
        # 提前付掉，属于本任务定义里的"预热失败"。
        raise RuntimeError(f"MCP 共享连接建立失败: {settings.mcp_server_url}")


def _warm_llm_transport() -> None:
    """LLM/后台记忆客户端共用的 httpx 传输层(耗时大头:SSL 证书链加载)。

    只按 settings 里实际会被用到的几个 timeout 分桶预热；哪些调用点用哪个
    timeout 见 app/agent/chat.py(self.client / _bg_client)与
    app/resilience/factory.py(primary/secondary)。不传 timeout 的调用点
    (如未启用容错时的 self.client)落在 get_shared_http_client(None) 那桶。
    """
    from app.config.settings import settings
    from app.observability.http_pool import get_shared_http_client
    get_shared_http_client(None)
    get_shared_http_client(settings.llm_timeout_s)
    get_shared_http_client(settings.memory_bg_timeout_s)


def _warm_faq_cache() -> None:
    """FAQ 语义缓存:只读本地 JSON 文件，不发网络请求(embedder 是惰性的，
    只在真正 lookup/add 时才建)。"""
    from app.agent.faq_cache import get_faq_cache
    get_faq_cache()


def _warm_kb_retriever() -> None:
    """本地向量索引 + embedder 客户端构造。绝不触达外部 ApeRAG——
    `kb_backend=aperag` 时本地索引只是三级降级链路的最后一级，与 ApeRAG
    是否可达完全无关；ApeRAG 在本机当前不可达也不会让这一步挂起或失败。
    本地索引文件不存在(从未构建过)会抛 FileNotFoundError，属于正常的
    "没什么可暖"，由外层统一按"这一步没成功"计入结果，不特殊掩盖。
    """
    from app.agent.tools.knowledge import warm_local_retriever
    warm_local_retriever()


def _warm_aperag() -> None:
    """L3(实跑发现,补进预热清单):ApeRAG 自身刚启动后的**第一次**查询实测
    2.3~3.7s 稳态之外还有一次冷启动尖峰,足以超过 `aperag_timeout_s`(默认
    10s)——如果第一次真正打到它的是买家的检索请求,这一轮买家会因超时拿
    不到任何知识注入(该项目按设计不做本地兜底,`kb_local_fallback_enabled`
    默认关)。这一步在服务启动阶段先替买家发一次"空跑"查询,把 ApeRAG 自己
    的冷启动成本挪到这里付,不让第一个真实用户买单——与本文件其它几步同一
    姿态。选择"加一步预热"而不是"调大 aperag_timeout_s":调大只是把买家
    等待的上限拉长,并不能让第一个用户少等;预热才是把这段冷启动成本从"买家
    的这一轮"里搬走。

    只在 `kb_backend == "aperag"` 时才发起(与 `_warm_kb_retriever` 反过来的
    条件对称:那一步绝不碰 ApeRAG,这一步只在会真正用到 ApeRAG 时才碰它);
    查询内容不重要,只为触发一次真实的检索往返。ApeRAG 不可达/超时会被
    warm_process() 按"这一步没成功"计入结果,不影响服务启动或其它步骤。
    """
    from app.config.settings import settings
    if settings.kb_backend != "aperag":
        return
    from app.agent.recall.external_kb import aperag_search
    rows = aperag_search("预热")
    if rows is None:
        raise RuntimeError("ApeRAG 预热查询未返回结果(不可达或超时)")


# 每步 (名字, 函数) —— 顺序即失败时的排查顺序;互相独立,单步失败不连累其它步骤。
_STEPS: list[tuple[str, Callable[[], None]]] = [
    ("mcp", _warm_mcp),
    ("llm_transport", _warm_llm_transport),
    ("faq_cache", _warm_faq_cache),
    ("kb_retriever", _warm_kb_retriever),
    ("aperag", _warm_aperag),
]


def warm_process(emit: Callable[[dict], None] | None = None) -> dict:
    """同步跑完所有预热步骤,每步独立 try/except。

    返回汇总结果(供调用方/测试内省);正常使用场景是在后台线程里调用，
    没有人会去取返回值——可观测性因此不能只靠返回值，必须在这里就把
    耗时/成败发出去(print + logger.info，外加可选的 emit 回调供上层接
    其它观测通道)。
    """
    results: dict[str, dict] = {}
    t0 = time.time()
    for name, fn in _STEPS:
        step_t0 = time.time()
        try:
            fn()
            results[name] = {"ok": True, "seconds": round(time.time() - step_t0, 3)}
        except Exception as exc:  # noqa: BLE001 单步失败不连累其它步骤,也不影响服务启动
            results[name] = {"ok": False, "seconds": round(time.time() - step_t0, 3),
                             "error": str(exc)[:200]}
            logger.warning("启动预热步骤 %s 失败(服务不受影响,仅这一项优化未生效)",
                           name, exc_info=True)

    total = round(time.time() - t0, 3)
    ok = all(r["ok"] for r in results.values())
    summary = {"total_seconds": total, "ok": ok, "steps": results}

    # 可观测性信号(W1 铁律):不管有没有接 langfuse/tracer,至少落一条 print——
    # 与 SessionManager reaper 回收会话时的打印同姿态,这类"后台悄悄跑一件事"
    # 的代码在本项目里反复因为"没人知道它到底跑没跑/跑成没跑成"被咬过。
    print(f"🔥 [启动预热] 总耗时 {total:.2f}s，{'全部成功' if ok else '部分失败'}：{results}",
         flush=True)
    logger.info("startup warmup finished: %s", summary)

    if emit is not None:
        try:
            emit({"type": "startup_warmup", **summary})
        except Exception:  # noqa: BLE001 观测回调出错不能反过来影响预热结果
            pass
    return summary


def warm_process_in_background() -> threading.Thread:
    """后台守护线程跑预热,立即返回——绝不阻塞 create_app()/服务就绪。

    调用方(app.py create_app())只需要拿到这个线程对象做测试内省
    (是否已启动/是否存活),不需要等它跑完。
    """
    t = threading.Thread(target=warm_process, daemon=True, name="startup-warmup")
    t.start()
    return t
