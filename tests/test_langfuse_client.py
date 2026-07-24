"""Langfuse 接入门控:默认关闭返回原生客户端;开而未配/未装时静默回退。"""

from openai import OpenAI

from app.config.settings import settings
from app.observability.langfuse_client import make_openai_client


def test_disabled_returns_native_client(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    c = make_openai_client(api_key="k", base_url="http://x/v1")
    assert type(c) is OpenAI          # 精确原生类型,零包装


def test_enabled_returns_openai_compatible(monkeypatch):
    """开了开关:返回 langfuse 包装(装了)或原生(没装/异常),两者都必须兼容 OpenAI 接口。"""
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-test")
    c = make_openai_client(api_key="k", base_url="http://x/v1")
    assert isinstance(c, OpenAI)      # langfuse.openai.OpenAI 是其子类
    assert hasattr(c.chat.completions, "create")
