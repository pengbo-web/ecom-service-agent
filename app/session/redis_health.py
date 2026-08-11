"""Redis 客户端的**有界失败**:强制超时 + 失败后退避冷却。

这个模块的存在理由是一次实测:本机 6379 上留着一个已删容器的 Docker 端口转发,
它**接受 TCP 连接但永不应答**。这是最坏的一种故障形态——`ConnectionRefused`
毫秒级就返回,而"接受后沉默"会让每一次操作耗满 socket 超时。

当时买家每轮对话的实测代价:

    「你好」(规则快路径,零 LLM 调用)   12.2 秒
    切成 file 会话后端后同一句           2.2 秒
    四处 Redis 消费方全部绕开后          ~0.2 秒

也就是说 12 秒里有 12 秒是 Redis,和模型、和检索、和 Agent 都没关系。

四个消费方各自都写了 `except Exception` 的 fail-soft,所以**功能上完全正常**——
没有报错、没有 500、买家照样收到回复。问题在于:

  1. `redis.from_url(url)` 不传超时,redis-py 默认 `socket_timeout=None`,
     意思是**无限阻塞**。买家热路径的最坏情况因此是无界的,而"最坏情况无界"
     在一个有 SLA 的在线客服里等于没有 SLA。
  2. 只有会话存储(store.py)做了失败后退避,会话锁与 hmdp 身份解析没有,
     于是故障期间**每个请求**都要重新连一次、再付一次超时。

换句话说:降级路径在正确性上是软的,在延迟上是硬的。这个模块把两件事都补上,
并且只写一份——四处各自实现一遍退避,迟早会漂移出三种不同的行为。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def make_client(url: str, *, connect_timeout: Optional[float] = None,
                socket_timeout: Optional[float] = None):
    """建一个**超时一定被设上**的 Redis 客户端。

    不接受"用调用方给的默认值":超时是这个函数存在的全部意义,漏传就取配置,
    配置没有就取模块默认——任何路径都不会落到 redis-py 的 `None`(无限阻塞)。

    Windows 下 localhost 会先解析成 IPv6 ::1 而 Redis 只监听 IPv4 → 连接超时。
    归一成 127.0.0.1 这一步原先只写在 hmdp_identity 里,另外三处没有;既然
    客户端构造集中到这里,这个修正也就自然对四处都生效了。
    """
    import redis
    from app.config.settings import settings

    ct = connect_timeout if connect_timeout is not None else float(
        getattr(settings, "redis_connect_timeout_s", 0.5))
    st = socket_timeout if socket_timeout is not None else float(
        getattr(settings, "redis_socket_timeout_s", 1.0))
    return redis.from_url((url or "").replace("localhost", "127.0.0.1"),
                          socket_connect_timeout=ct, socket_timeout=st)


class Breaker:
    """失败后退避:用一个时间戳,不起线程。

    与 `store.py` 里既有的 `_down_until` 是同一套做法(那份先写、有回归钉着,
    不动它;这里给还没有退避的消费方用同一个语义,而不是各写一遍)。

    为什么是时间戳而不是看门狗线程:冷却期内的判断只是一次浮点比较,几乎零
    成本,也不给进程添一个需要关闭的后台线程。
    """

    def __init__(self, cooldown_s: float, name: str = "Redis",
                 dependency: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._cooldown = float(cooldown_s)
        self._name = name
        # 挂掉的那个依赖叫什么。**不能写死成 "Redis"**:这个类现在也被 ApeRAG
        # 检索用着,实跑时日志打出来是「ApeRAG 检索 降级:Redis 不可用……请尽快
        # 恢复 Redis」——运维照这条去重启 Redis,而挂的是 ApeRAG。一条把人指错
        # 方向的告警比没有告警更糟。默认跟随 name,调用方可单独指定。
        self._dep = dependency or name
        # 单调时钟:不受系统时间被人为/NTP 调整影响(与 store.py 同理)。
        self._clock = clock
        self._down_until = 0.0

    @property
    def open(self) -> bool:
        """True = 正在冷却,这次**不要**碰 Redis。"""
        return self._clock() < self._down_until

    def record_failure(self, op: str, exc: BaseException) -> None:
        """记一次失败并开冷却窗口。大声警告,绝不静默——降级必须有出口。"""
        first = not self.open
        self._down_until = self._clock() + self._cooldown
        if first:
            # 只在"从可用跌到不可用"这一刻打 warning,冷却期内的后续跳过不再刷屏:
            # 一条淹在几千条重复里的告警等于没有告警。
            logger.warning(
                "%s 降级:%s 不可用(%s: %s),op=%s;本次已按 fail-soft 处理,"
                "%.1fs 冷却期内不再尝试连接(避免每个请求都重复承担一次超时),"
                "到期自动重试、恢复即自动切回。"
                "【运维须知】这不是噪音——降级期间该路径的能力是缺失的,请尽快恢复 %s。",
                self._name, self._dep, type(exc).__name__, exc, op,
                self._cooldown, self._dep)

    def record_success(self) -> None:
        """成功一次就立刻关掉冷却:恢复不必等窗口自然到期。"""
        self._down_until = 0.0
