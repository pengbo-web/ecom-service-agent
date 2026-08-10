"""内网服务间调用的 httpx 客户端:按目标地址决定要不要走系统代理。

**它修的是一个真实且完全静默的故障。**

httpx 默认 `trust_env=True`,会读 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量。开发机
上常年挂着科学上网代理(本机实测 `HTTP_PROXY=http://127.0.0.1:7897`),而
`NO_PROXY` 是否包含回环地址取决于**是谁启动的这个进程**——从终端起的服务
往往有,从 IDE / 服务管理器 / 容器编排起的往往没有。

结果:`http://127.0.0.1:8085/product/list` 这种明明在同一台机器上的调用,被
塞进代理隧道,超时。而调用点写的是

    try: ...
    except Exception: return {"products": []}

于是商城页面显示"0 件商品",没有报错、没有日志、没有任何线索。买家看到的是
一个空店铺,运维看到的是一切正常。

企业部署踩这个坑只会更狠:公司网络的出口代理不可能路由到内网服务地址,而
`NO_PROXY` 少写一条内网网段就会让整个商品/订单链路静默失效。

---

**规则:目标是回环或私有地址 → 不走代理;公网地址 → 保持默认行为。**

不粗暴地全局 `trust_env=False`:ApeRAG、模型网关这类**可能真的部署在公网**
的依赖,在公司网络里恰恰**必须**走代理才出得去。一刀切会把这一类打死。

判据用 `ipaddress` 判真实地址段,不做字符串前缀匹配——`"10.0.0.1".startswith("10.")`
这种写法会把 `10abc.example.com` 也判成内网,而域名解析出来可能在公网。
无法解析成 IP 的主机名(如 `aperag.internal`)保守按**公网**处理:判不准时
维持默认行为,不去改变一个原本能工作的链路。
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def is_internal_host(url: str) -> bool:
    """目标是不是回环/私有地址(据此决定绕过代理)。解析不出来一律 False。"""
    host = urlparse(url).hostname or ""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 主机名而非字面 IP。只认 localhost 这一个特例:它在所有系统上都解析到
        # 回环。其余名字不在这里做 DNS——解析要花时间,而这个函数在请求路径上。
        return host.lower() in ("localhost", "localhost.localdomain")
    return ip.is_loopback or ip.is_private or ip.is_link_local


def internal_client(url: str, timeout: float = 3.0, **kwargs) -> httpx.Client:
    """给 `url` 建一个客户端:内网地址绕过系统代理,公网地址保持默认。

    调用方仍需自己 `with` 起来(与改造前的写法一致,不改变连接生命周期)。
    """
    trust_env = not is_internal_host(url)
    return httpx.Client(timeout=timeout, trust_env=trust_env, **kwargs)


def internal_async_client(url: str, timeout: float = 30.0, **kwargs):
    """`internal_client` 的 async 版本,给需要注入 `httpx.AsyncClient` 的库用
    (如 MCP SDK 的 `streamable_http_client(url, http_client=...)`)。

    单独一个函数而不是给 `internal_client` 加参数:两者返回的类型不同,
    合成一个会逼调用方看参数才知道拿到的是同步还是异步客户端。

    超时默认给得比同步版宽松:MCP 是长连接会话(建连后要保持存活等待调用),
    用 3 秒的读超时会把正常的空闲连接掐断。
    """
    trust_env = not is_internal_host(url)
    return httpx.AsyncClient(timeout=timeout, trust_env=trust_env, **kwargs)


def warn_if_proxy_would_break(url: str) -> None:
    """内网地址却检测到代理环境变量时记一条 warning。

    单独一个函数而不是塞进 `internal_client`:失败时才需要这条线索,而
    `internal_client` 在成功路径上也会被调用,每次都记日志只会淹没真正的问题。
    调用方在 except 分支里调它,把"连不上"和"你机器上挂着代理"这两件事
    连起来——否则运维看到的只是一个超时,不会想到代理头上。
    """
    import os

    if not is_internal_host(url):
        return
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if proxy:
        logger.warning(
            "目标 %s 是内网地址,但当前进程设置了 HTTP_PROXY=%s。"
            "若本次失败是超时,极可能是请求被塞进了代理隧道——"
            "本项目对内网地址已默认绕过代理,若仍失败请检查是否有其它代理配置"
            "(如 ALL_PROXY / 系统级代理)。", url, proxy)
