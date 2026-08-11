"""ApeRAG 不可用时的退避冷却:降级要 fail-fast,不能每轮重付一次超时。

实测(2026-08-11):ApeRAG 容器随 Docker 引擎一起挂掉,一句需要知识库的问句
(「退货运费谁承担」)在 `recall` 事件上花了 **14.7 秒**——而 `aperag_timeout_s`
配的是 6 秒。差在 httpx 的 `Timeout(6.0)` 是 connect/read/write/pool **各** 6 秒,
不是总额。

没有冷却时,每一个需要知识库的轮次都要重付一次这十几秒。这与会话存储/会话锁/
hmdp 身份解析是同一个病(见 tests/test_redis_bounded_failure.py):fail-soft 只
保证不崩,不保证不慢。

**冷却不能改变降级语义**:冷却期内返回 None,与真实故障完全同一个出口——
kb.py 据此回落本地索引,并照常打 `degraded` 标记进看板的知识库降级率。这一点
必须被钉住,否则"少等十几秒"就变成了"故障被藏起来"。
"""

import pytest

from app.agent.recall import external_kb as ek
from app.session.redis_health import Breaker


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture()
def wired(monkeypatch):
    """把 ApeRAG 配置补齐(否则走"缺 api_key"那条早退),并换上假时钟的 breaker。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "aperag_api_key", "k")
    monkeypatch.setattr(st.settings, "aperag_collection_id", "c")
    monkeypatch.setattr(st.settings, "aperag_base_url", "http://127.0.0.1:8000")
    clk = FakeClock()
    monkeypatch.setattr(ek, "_breaker", Breaker(30.0, clock=clk))
    return clk


def _count_calls(monkeypatch, exc=None, status=200, payload=None):
    """替掉 internal_client,记调用次数;exc 非空则每次抛。"""
    calls = {"n": 0}

    class Resp:
        status_code = status
        text = "boom"

        @staticmethod
        def json():
            return payload or {"items": []}

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            calls["n"] += 1
            if exc is not None:
                raise exc
            return Resp()

    monkeypatch.setattr(ek, "internal_client", lambda *a, **kw: C())
    return calls


def test_expensive_failure_opens_cooldown(wired, monkeypatch):
    """连接失败/超时这一类(贵的)要开冷却:之后的轮次不再发起调用。"""
    import httpx
    calls = _count_calls(monkeypatch, exc=httpx.ConnectError("refused"))

    for _ in range(4):
        assert ek.aperag_search("退货运费谁承担") is None
    assert calls["n"] == 1, (
        f"冷却期内不该再调 ApeRAG,实际调了 {calls['n']} 次"
        "——每次都是十几秒摞在买家这一轮上")


def test_cooldown_expires_and_retries(wired, monkeypatch):
    """不能永久停在降级态:冷却到期要再试,服务恢复才能自动切回。"""
    import httpx
    calls = _count_calls(monkeypatch, exc=httpx.ConnectError("refused"))
    ek.aperag_search("q")
    assert calls["n"] == 1
    wired.advance(31.0)
    ek.aperag_search("q")
    assert calls["n"] == 2


def test_cooldown_keeps_the_same_degraded_semantics(wired, monkeypatch):
    """冷却期内的返回值必须与真实故障一致(None)。

    这是本组最重要的一条:None 是 kb.py 判"服务不可用 → 回落本地 + 打 degraded"
    的唯一依据。若冷却期内返回 [](空结果)而不是 None,故障就会被误报成
    "知识库正常但这一问没有政策",看板上的降级率归零——那是把故障藏起来。
    """
    import httpx
    _count_calls(monkeypatch, exc=httpx.ConnectError("refused"))
    ek.aperag_search("q")                      # 开冷却
    assert ek.aperag_search("q") is None, "冷却期内必须返回 None,不能返回 []"


def test_http_error_does_not_open_cooldown(wired, monkeypatch):
    """非 200 是"很快回来了、只是回了个错":代价是一次往返而不是一次超时,
    不该为它压掉 30 秒的恢复探测。"""
    calls = _count_calls(monkeypatch, status=500)
    for _ in range(3):
        assert ek.aperag_search("q") is None
    assert calls["n"] == 3, "便宜的失败应该照常重试,不进冷却"


def test_success_closes_cooldown(wired, monkeypatch):
    """恢复不必等窗口自然到期。"""
    import httpx
    calls = _count_calls(monkeypatch, exc=httpx.ConnectError("x"))
    ek.aperag_search("q")
    assert ek._kb_breaker().open

    wired.advance(31.0)
    _count_calls(monkeypatch, status=200,
                 payload={"items": [{"source": "policy.md", "content": "运费规则",
                                     "score": 0.9, "recall_type": "vector"}]})
    rows = ek.aperag_search("q")
    assert rows and rows[0]["doc"] == "policy"
    assert not ek._kb_breaker().open, "成功一次就该立刻关掉冷却"


def test_healthy_service_is_never_skipped(wired, monkeypatch):
    """退避不能影响正常路径:健康时每一轮都要真的去检索。"""
    calls = _count_calls(monkeypatch, status=200)
    for _ in range(3):
        assert ek.aperag_search("q") == []
    assert calls["n"] == 3
