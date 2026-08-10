"""内网调用绕过系统代理。

**这个缺陷是实跑走查抓到的。** 现场表现:商城页显示"0 件商品",没有报错、
没有日志;买家看到一个空店铺,运维看到一切正常。根因是开发机上挂着
`HTTP_PROXY=http://127.0.0.1:7897`,而启动服务的那个进程环境里没有把回环地址
写进 `NO_PROXY`——httpx 默认 `trust_env=True`,于是 `http://127.0.0.1:8085`
这种同机调用被塞进代理隧道超时,再被调用点的 `except: return []` 吃掉。

企业部署踩这个坑只会更狠:公司出口代理不可能路由到内网服务地址。
"""

from __future__ import annotations

import pytest

from app.net.internal_http import (internal_async_client, internal_client,
                                   is_internal_host)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8085/product/list",
    "http://localhost:8085/x",
    "http://192.168.1.10:8080/x",
    "http://10.0.0.5/x",
    "http://172.16.3.4/x",
    "http://[::1]:8085/x",
])
def test_loopback_and_private_are_internal(url):
    assert is_internal_host(url) is True


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "https://aperag.example.com/api",
    "http://8.8.8.8/x",
])
def test_public_addresses_are_not_internal(url):
    """公网依赖在公司网络里恰恰**必须**走代理才出得去。

    一刀切 `trust_env=False` 会把模型网关、外部 KB 这一类全打死——所以判据
    是地址段,不是"是不是我们自己的服务"。
    """
    assert is_internal_host(url) is False


def test_unresolvable_hostname_defaults_to_public():
    """判不准时维持默认行为,不去改变一个原本能工作的链路。

    `aperag.internal` 这种名字可能解析到内网也可能不是,这里不做 DNS
    (解析要花时间,而这个判断在请求路径上)。保守按公网处理。
    """
    assert is_internal_host("http://aperag.internal/api") is False


def test_startswith_style_matching_would_be_wrong():
    """不能用字符串前缀判内网。

    `"10abc.example.com".startswith("10.")` 是 False,但反过来
    `"10.evil.com"` 这类名字用朴素前缀匹配会被误判成 10/8 内网段,
    而它解析出来可能在公网。所以用 ipaddress 判真实地址段。
    """
    assert is_internal_host("http://10.evil.com/x") is False
    assert is_internal_host("http://127.0.0.1.attacker.com/x") is False


def test_internal_client_disables_trust_env_only_for_internal():
    with internal_client("http://127.0.0.1:8085/x") as c:
        assert c.trust_env is False
    with internal_client("https://api.openai.com/v1") as c:
        assert c.trust_env is True


def test_hmdp_client_uses_internal_client():
    """AI 全部 hmdp 工具的唯一出口必须走这条路径。

    改造前 `HmdpClient._call` 直接 `httpx.request(...)`,代理一挂,每个工具的
    降级文案都是"无法连接 hmdp"——Agent 拿到一句错误,买家拿到一段临场编出来
    的解释。
    """
    import inspect

    from mcp_server import hmdp_client

    src = inspect.getsource(hmdp_client.HmdpClient._call)
    assert "internal_client" in src
    assert "httpx.request" not in src, "不该再有绕过内网判定的裸调用"


def test_product_endpoints_do_not_swallow_failures_silently():
    """商品接口失败必须留日志 + 下发 degraded,不能只 `return []`。

    空列表有两种完全不同的含义(店里真没商品 / 商品服务连不上),共用一种
    表示会让一次内网故障在买家眼里变成"这家店是空的"。
    """
    import inspect

    from app.api import app as app_module

    src = inspect.getsource(app_module)
    assert '"degraded": True' in src, "商品列表失败要下发 degraded 标记"
    assert "warn_if_proxy_would_break" in src, "失败时要把代理这条线索连上"


def test_async_client_follows_the_same_rule():
    """MCP 走 async 客户端,判定规则必须与同步版一致。"""
    import asyncio

    async def _check():
        c1 = internal_async_client("http://127.0.0.1:9123/mcp")
        c2 = internal_async_client("https://mcp.example.com/mcp")
        try:
            assert c1.trust_env is False
            assert c2.trust_env is True
        finally:
            await c1.aclose()
            await c2.aclose()

    asyncio.run(_check())


def test_mcp_client_injects_internal_http_client():
    """MCP 连接必须注入内网客户端。

    这条不是形式检查。SDK 默认建的 `httpx.AsyncClient` 是 `trust_env=True`,
    代理环境下连不上 `127.0.0.1:9123`,而 `_init_mcp` 会**静默降级到本地工具**。
    后果不是"少几个工具":本地工具读 agent 自己的订单库,页面读 hmdp——实测
    同一个买家,AI 说"您名下共 2 笔订单",他自己的订单页里有 9 笔;AI 还会
    回答"未找到订单 ORD-20240115-001",而那笔单在 hmdp 里是已发货状态。
    """
    import inspect

    from app.mcp_client.client import MCPClient

    src = inspect.getsource(MCPClient._run)
    assert "internal_async_client" in src
    assert "http_client=http_client" in src, "必须把客户端真的传给 SDK"
