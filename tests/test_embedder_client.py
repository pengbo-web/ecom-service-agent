"""Embedder 客户端超时/重试配置:热路径快速失败,离线路径保留 SDK 默认。

背景:KB 预召回在每轮回复前必经 embedding,SDK 默认 600s 超时 + 2 次重试
曾在后台路径实测累积出单次 906s 挂起(见 settings.memory_bg_timeout_s 注释)。
热路径必须显式短超时 + 零重试;不传参数的构造点(离线建索引)行为不变。
"""

from app.agent.rag.embedder import Embedder


def test_embedder_passes_timeout_and_retries_to_client():
    e = Embedder(api_key="x", base_url="http://localhost", model="m",
                 timeout=6.0, max_retries=0)
    assert e._client.timeout == 6.0
    assert e._client.max_retries == 0


def test_embedder_defaults_keep_sdk_behavior():
    e = Embedder(api_key="x", base_url="http://localhost", model="m")
    # 不传 kwargs 时不得覆盖 SDK 默认(离线索引构建等场景):max_retries 应为 SDK 默认 2
    assert e._client.max_retries == 2
    # timeout 保持 SDK 默认对象(非显式 float),即未被本封装显式覆盖
    assert e._client.timeout != 6.0


def test_embedder_routes_through_langfuse_wrapper(monkeypatch):
    """阶段一 gap⑤:Embedder 必须经 make_openai_client 构造底层客户端——
    门控关/未装时行为与裸 OpenAI(**kwargs) 完全一致(见下面两个既有测试),
    门控开时 embedding 调用才有机会被自动上报。"""
    captured = {}

    def _fake_make_client(**kwargs):
        captured.update(kwargs)
        return "sentinel-client"

    monkeypatch.setattr("app.agent.rag.embedder.make_openai_client", _fake_make_client)
    e = Embedder(api_key="x", base_url="http://localhost", model="m", timeout=6.0, max_retries=0)
    assert e._client == "sentinel-client"
    assert captured == {"api_key": "x", "base_url": "http://localhost",
                        "timeout": 6.0, "max_retries": 0}


def test_embedder_degrades_silently_when_langfuse_init_raises(monkeypatch):
    """核心 fail-soft 性质:门控开着但 Langfuse 初始化抛异常,Embedder 仍必须
    正常拿到一个可用客户端(回退原生 OpenAI),不能因观测层异常而构造失败。"""
    import os
    from app.config.settings import settings
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    # make_openai_client 在门控开时无条件 os.environ.setdefault(LANGFUSE_HOST,...)——
    # 让这一步抛异常,模拟"包装初始化本身失败"的真实后果。
    monkeypatch.setattr(os.environ, "setdefault",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    e = Embedder(api_key="x", base_url="http://localhost", model="m",
                 timeout=6.0, max_retries=0)
    assert e._client.timeout == 6.0
    assert e._client.max_retries == 0


def test_hot_path_retriever_uses_fast_fail_settings(monkeypatch):
    """_get_retriever 构造的 Embedder 必须带 recall_kb 超时/零重试。"""
    import app.agent.tools.knowledge as knowledge_mod
    from app.config.settings import settings

    captured = {}

    class _FakeRetriever:
        def __init__(self, embedder, backend):
            captured["embedder"] = embedder

        def load(self):
            pass

    monkeypatch.setattr(knowledge_mod, "KnowledgeRetriever", _FakeRetriever)
    monkeypatch.setattr(knowledge_mod, "_create_backend_from_settings", lambda: object())
    knowledge_mod.reset_retriever()
    try:
        knowledge_mod._get_retriever()
        client = captured["embedder"]._client
        assert client.timeout == settings.recall_kb_timeout_s
        assert client.max_retries == settings.recall_kb_embed_retries
    finally:
        knowledge_mod.reset_retriever()
