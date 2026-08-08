"""W1 服务化 L2:进程级共享 httpx.Client 池——验证共享带来的性能收益，
以及"两个并发会话不会互相看到对方状态"这条安全性质。
"""

import threading

import httpx
import pytest

from app.observability import http_pool
from app.observability.langfuse_client import make_openai_client


@pytest.fixture(autouse=True)
def _reset_pool():
    http_pool.reset_http_pool()
    yield
    http_pool.reset_http_pool()


def test_same_timeout_returns_same_client():
    a = http_pool.get_shared_http_client(30.0)
    b = http_pool.get_shared_http_client(30.0)
    assert a is b


def test_different_timeout_returns_different_client():
    a = http_pool.get_shared_http_client(30.0)
    b = http_pool.get_shared_http_client(60.0)
    assert a is not b


def test_none_timeout_is_a_stable_bucket():
    a = http_pool.get_shared_http_client(None)
    b = http_pool.get_shared_http_client()
    assert a is b


def test_reset_clears_pool():
    a = http_pool.get_shared_http_client(30.0)
    http_pool.reset_http_pool()
    b = http_pool.get_shared_http_client(30.0)
    assert a is not b


def test_concurrent_construction_yields_single_client():
    """多线程同时首次取同一个 timeout 桶,不应各自建出不同的 httpx.Client
    (锁保护的双检查,与 get_shared_mcp_client 同姿态)。"""
    http_pool.reset_http_pool()
    results: list[httpx.Client] = []
    lock = threading.Lock()

    def work():
        c = http_pool.get_shared_http_client(15.0)
        with lock:
            results.append(c)

    threads = [threading.Thread(target=work) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 20
    assert len({id(c) for c in results}) == 1


def test_make_openai_client_reuses_shared_transport(monkeypatch):
    """两次 make_openai_client 用相同 timeout → 底层 httpx.Client 是同一个对象
    (这是本次优化的核心:免每次都重建 SSL 上下文)。"""
    from app.config.settings import settings
    monkeypatch.setattr(settings, "langfuse_enabled", False)

    c1 = make_openai_client(api_key="k1", base_url="https://example.com/v1", timeout=33.0)
    c2 = make_openai_client(api_key="k2", base_url="https://example.com/v1", timeout=33.0)
    assert c1._client is c2._client


def test_make_openai_client_respects_explicit_http_client(monkeypatch):
    """调用方自己传了 http_client(如测试注入 mock)时,不能被这层共享逻辑覆盖。"""
    from app.config.settings import settings
    monkeypatch.setattr(settings, "langfuse_enabled", False)

    own = httpx.Client()
    try:
        c = make_openai_client(api_key="k", base_url="https://example.com/v1", http_client=own)
        assert c._client is own
    finally:
        own.close()


def test_hot_reload_semantics_unaffected_by_sharing(monkeypatch):
    """共享的只是底层传输层;api_key/base_url 仍必须按调用时的最新配置构造，
    不能被共享传输"固化"成第一次构造时的值(否则 config_reload 热更新
    fallback_base_url 后,新会话会悄悄继续用旧地址——这是本次改动明确
    不能引入的行为变化)。"""
    from app.config.settings import settings
    monkeypatch.setattr(settings, "langfuse_enabled", False)

    c1 = make_openai_client(api_key="key-old", base_url="https://old.example.com/v1", timeout=77.0)
    c2 = make_openai_client(api_key="key-new", base_url="https://new.example.com/v1", timeout=77.0)

    assert c1._client is c2._client            # 传输层共享
    assert str(c1.base_url) != str(c2.base_url)  # 但各自的配置互不影响
    assert c1.api_key == "key-old"
    assert c2.api_key == "key-new"


def _fake_chat_response() -> dict:
    return {
        "id": "chatcmpl-x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
    }


def test_concurrent_sessions_do_not_observe_each_others_identity(monkeypatch):
    """核心安全测试:两个"会话"的 OpenAI 客户端共享同一个 httpx.Client 传输层，
    并发发起大量请求时，各自的 Authorization/目标地址必须严格保持隔离——
    共享的是无状态的连接池,不是任何会话状态。"""
    from app.config.settings import settings
    monkeypatch.setattr(settings, "langfuse_enabled", False)

    seen: list[tuple[str, str]] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append((request.headers.get("authorization", ""), str(request.url)))
        return httpx.Response(200, json=_fake_chat_response())

    shared = httpx.Client(transport=httpx.MockTransport(handler), timeout=99.0)
    http_pool.set_shared_http_client_for_test(99.0, shared)

    client_a = make_openai_client(api_key="key-A", base_url="https://host-a.example/v1", timeout=99.0)
    client_b = make_openai_client(api_key="key-B", base_url="https://host-b.example/v1", timeout=99.0)

    assert client_a._client is shared
    assert client_b._client is shared
    assert client_a is not client_b

    errors: list[Exception] = []

    def fire(client, n=25):
        try:
            for _ in range(n):
                client.chat.completions.create(
                    model="m", messages=[{"role": "user", "content": "hi"}])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t_a = threading.Thread(target=fire, args=(client_a,))
    t_b = threading.Thread(target=fire, args=(client_b,))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)

    assert not errors
    auth_for_a = {a for a, u in seen if "host-a.example" in u}
    auth_for_b = {a for a, u in seen if "host-b.example" in u}
    assert auth_for_a == {"Bearer key-A"}
    assert auth_for_b == {"Bearer key-B"}
    assert len(seen) == 50
