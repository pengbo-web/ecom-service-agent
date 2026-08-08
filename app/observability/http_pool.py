"""进程级共享 httpx.Client 池：免每个 OpenAI 客户端各自建一次 SSL 上下文。

背景(W1 服务化 L2 测量):新会话构造平均 ~0.86s，用 cProfile 定位后发现
98% 以上耗在 `ssl.create_default_context()` → `load_verify_locations()`——
也就是每个新 `OpenAI(...)`/`Embedder(...)` 客户端各自新建一个 `httpx.Client`
时，从磁盘重新加载一遍证书链建 SSL 上下文，而不是任何网络往返。

openai SDK 允许通过 `http_client=` 注入自定义传输层，且官方明确支持多个
`OpenAI()` 实例共享同一个 `httpx.Client`（连接池复用是 httpx 的标准用法）。
按 timeout 分桶缓存后，同一 timeout 配置全进程只建一次 SSL 上下文。

安全性(为什么可以在会话间共享):
- `httpx.Client` 本身不持有任何用户/会话数据——每次请求的 URL/headers/body
  都由上层 `OpenAI()` 客户端在调用那一刻显式传入；这里复用的只是连接池与
  已经建好的 SSL 上下文，不会在会话之间泄露任何内容。
- `api_key`/`base_url`/`model` 这些随会话可能不同的配置，仍然是每次
  `make_openai_client()` 调用时传给上层 `OpenAI()` 的构造参数——**不会**
  被这层共享传输"固化"，配置热更新(见 app/config/hot_reload.py)语义与
  改动前完全一致：新会话仍然读当时最新的 settings 构造 `OpenAI()` 外壳，
  只是外壳内部的 socket/SSL 层被复用。
- 没有任何调用点会对这个共享 client 调 `.close()`（EcomAgent.close() 只关
  `tool_manager`，从不关 `self.client`），所以不存在"一个会话关闭连接、
  其它会话的请求跟着断"的教训（与 memory_tool 的串户教训是两类问题：
  那边是**用户数据**被共享单例覆盖，这里是**无状态传输层**被复用，
  下面 `get_shared_http_client` 的返回值里不含任何字段是按用户/会话
  区分的)。
"""

from __future__ import annotations

import threading

import httpx

_lock = threading.Lock()
_pool: dict[object, httpx.Client] = {}

# dict key 不能用 None 本身重复判断(仍可以，但显式一个哨兵更不容易踩坑)
_DEFAULT_KEY = "__default_timeout__"


def get_shared_http_client(timeout: float | None = None) -> httpx.Client:
    """按 timeout 分桶取（或建）进程级共享的 httpx.Client。

    `timeout=None` 表示调用方没有显式指定超时(退回 openai SDK 自己的默认值)，
    落在同一个桶里，与"显式传了这个默认值"是同一把共享 client。
    """
    key = _DEFAULT_KEY if timeout is None else timeout
    client = _pool.get(key)
    if client is not None:
        return client
    with _lock:
        client = _pool.get(key)
        if client is None:
            kwargs: dict = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            client = httpx.Client(**kwargs)
            _pool[key] = client
        return client


def reset_http_pool() -> None:
    """清空缓存池(测试用)。刻意不主动 close 池中的 client——它们可能仍被
    进程里其它已构造好的 OpenAI 客户端持有，强行关闭会让那些客户端的下一次
    调用报连接已关闭；老化的 client 交给进程退出自然回收即可。"""
    with _lock:
        _pool.clear()


def set_shared_http_client_for_test(timeout: float | None, client: httpx.Client) -> None:
    """测试注入(绕过真实网络传输)。与
    `app.mcp_client.shared.set_shared_mcp_for_test` 同姿态：单测用它把某个
    timeout 桶的共享 client 换成基于 `httpx.MockTransport` 的假连接，验证
    "多个会话共享同一个传输层、但各自的身份/配置不串"这条安全性质。
    """
    key = _DEFAULT_KEY if timeout is None else timeout
    with _lock:
        _pool[key] = client
