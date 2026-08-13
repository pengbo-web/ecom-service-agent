"""内部 HTTP 客户端复用连接池:一个没有取舍的性能修复。

**实测**(压测时抓到)。`/api/products` 每次请求都 `httpx.Client(...)` 新建一个客户端,
`with` 退出就关。30 次 hmdp `/product/list`:

    每次新建客户端: p50 311ms  mean 332ms
    复用连接池    : p50  48ms  mean  56ms   ← 降 83%

那 ~276ms 差额主要**不是** TCP 握手(本机握手亚毫秒级),而是构造 `httpx.Client`
本身的开销(建 SSL context、读环境变量)。端到端压测印证:

    /api/products  并发1  p50 312ms → 15ms
                   并发20 p50 1205ms → 34ms (35×);QPS 12.0 → 47.1 (3.9×)

**没有取舍**:不缓存任何业务数据,所以不存在数据陈旧;只是别再为每个请求重造客户端。

两个设计要点各有一条测试守着:
- `with` 退出**不能**关掉共享池(12 个调用点全写着 `with`,关掉会让第二次调用起全失败);
- 传了除 `timeout` 之外的 kwargs 时**退回每次新建**——headers/limits/auth 绑在客户端
  实例上,共享出去等于让一个调用方的配置泄漏给别人。宁可慢,不要串配置。
"""

import httpx
import pytest

from app.net.internal_http import _PooledClient, internal_client, reset_shared_clients


@pytest.fixture(autouse=True)
def _clean():
    reset_shared_clients()
    yield
    reset_shared_clients()


def test_same_host_reuses_one_underlying_client():
    a = internal_client("http://127.0.0.1:8085/x", timeout=3.0)
    b = internal_client("http://127.0.0.1:8085/y", timeout=4.0)
    assert isinstance(a, _PooledClient) and isinstance(b, _PooledClient)
    assert a._c is b._c, "同一个 trust_env 应共用一个连接池"


def test_with_block_does_not_close_the_pool():
    """**核心断言。** 现有 12 个调用点全是 `with internal_client(...) as c:`,
    `with` 退出若关闭共享池,第二次调用起会全部失败。"""
    with internal_client("http://127.0.0.1:8085/x") as c:
        underlying = c._c
    assert not underlying.is_closed, "with 退出把共享池关了"

    with internal_client("http://127.0.0.1:8085/y") as c2:
        assert c2._c is underlying


def test_explicit_close_is_a_noop():
    """有调用方会显式 `close()`;那不该拆掉共享池。"""
    c = internal_client("http://127.0.0.1:8085/x")
    c.close()
    assert not c._c.is_closed


def test_internal_and_external_use_separate_pools():
    """内网绕代理、公网走代理,两者的 `trust_env` 不同,不能共用一个池。"""
    internal = internal_client("http://127.0.0.1:8085/x")
    external = internal_client("https://example.com/x")
    assert internal._c is not external._c
    assert internal._c.trust_env is False
    assert external._c.trust_env is True


def test_extra_kwargs_fall_back_to_a_fresh_client():
    """传了额外 kwargs → 不共享。

    headers/limits/auth 绑在客户端实例上,共享出去会让一个调用方的配置泄漏给其它
    调用方——那种 bug 表现为"另一个接口莫名带上了别人的 header",极难查。
    """
    c = internal_client("http://127.0.0.1:8085/x",
                        headers={"X-Test": "1"})
    assert isinstance(c, httpx.Client), "带自定义 kwargs 时仍被共享了"
    c.close()


def test_timeout_is_per_request_not_per_pool():
    """超时绑在包装上、每次请求单独传:否则每个不同超时值都要一个池,又回到没池。"""
    a = internal_client("http://127.0.0.1:8085/x", timeout=1.0)
    b = internal_client("http://127.0.0.1:8085/x", timeout=9.0)
    assert a._c is b._c
    assert a._timeout == 1.0 and b._timeout == 9.0


def test_pooled_client_forwards_verbs(monkeypatch):
    """包装必须把动词转发到底层客户端,并默认带上自己的 timeout。"""
    seen = []

    class FakeUnderlying:
        is_closed = False
        def get(self, url, **kw): seen.append(("get", url, kw)); return "R"
        def post(self, url, **kw): seen.append(("post", url, kw)); return "R"

    c = _PooledClient(FakeUnderlying(), timeout=7.0)
    assert c.get("/a") == "R"
    assert c.post("/b", json={"x": 1}) == "R"
    assert seen[0][2]["timeout"] == 7.0
    assert seen[1][2]["json"] == {"x": 1}


def test_caller_can_override_timeout_per_request():
    class FakeUnderlying:
        is_closed = False
        def get(self, url, **kw): return kw["timeout"]

    c = _PooledClient(FakeUnderlying(), timeout=7.0)
    assert c.get("/a", timeout=2.0) == 2.0, "单次请求的 timeout 应能覆盖默认值"


def test_reset_closes_pools():
    c = internal_client("http://127.0.0.1:8085/x")
    underlying = c._c
    reset_shared_clients()
    assert underlying.is_closed
    # 重置后再取要拿到新的池,而不是已关闭的那个
    assert internal_client("http://127.0.0.1:8085/x")._c is not underlying
