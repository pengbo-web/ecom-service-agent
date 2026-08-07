"""demo_curation.py 的客户端构造:阶段一 gap⑤——非容错分支必须经
make_openai_client 包装,门控关/未装/异常都要与裸 OpenAI(...) 行为一致。
不跑 main()(会真调 LLM/依赖 .env 凭据),只测 `_build_client()` 这个纯函数。
"""

from app.config.settings import settings
from app.scripts.demo_curation import _build_client


def test_build_client_routes_through_langfuse_wrapper_when_resilience_off(monkeypatch):
    monkeypatch.setattr(settings, "resilience_enabled", False)
    captured = {}

    def _fake_make_client(**kwargs):
        captured.update(kwargs)
        return "sentinel-client"

    monkeypatch.setattr("app.observability.langfuse_client.make_openai_client",
                        _fake_make_client)
    client = _build_client()
    assert client == "sentinel-client"
    assert captured["api_key"] == settings.openai_api_key


def test_build_client_degrades_silently_when_langfuse_disabled(monkeypatch):
    """门控关(默认态):必须拿到原生 OpenAI 客户端,行为与改动前一致。"""
    from openai import OpenAI
    monkeypatch.setattr(settings, "resilience_enabled", False)
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    client = _build_client()
    assert type(client) is OpenAI


def test_build_client_degrades_silently_when_langfuse_init_raises(monkeypatch):
    """核心 fail-soft 性质:门控开着但 Langfuse 初始化本身抛异常,仍必须
    拿到一个可用的原生 OpenAI 客户端,不能构造失败。"""
    import os
    from openai import OpenAI
    monkeypatch.setattr(settings, "resilience_enabled", False)
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(os.environ, "setdefault",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    client = _build_client()
    assert isinstance(client, OpenAI)
