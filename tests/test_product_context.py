from app.agent.runtime_context import set_current_item, get_current_item
from app.api.schemas import ChatRequest


def test_chat_request_has_current_item_id():
    r = ChatRequest(session_id="s", message="这是什么", current_item_id="155")
    assert r.current_item_id == "155"


def test_current_item_contextvar_roundtrip():
    set_current_item("155")
    assert get_current_item() == "155"
    set_current_item(None)
    assert get_current_item() is None


from app.agent.product_context import fetch_product_context


class _FakeResp:
    def __init__(self, js): self._js = js; self.status_code = 200
    def json(self): return self._js
    def raise_for_status(self): pass


class _FakeClient:
    def __init__(self, js): self._js = js
    def get(self, url, timeout=None): return _FakeResp(self._js)


def test_fetch_product_context_formats_block():
    js = {"success": True, "data": {"title": "过膝呢子大衣", "price": 30000,
          "stock": 5, "specs": "{\"颜色\":\"驼色\"}", "description": "宽松中长款"}}
    block = fetch_product_context("155", client=_FakeClient(js))
    assert block is not None
    assert "过膝呢子大衣" in block and "300" in block   # 分→元
    assert "这" in block                                 # 指代消解声明


def test_fetch_product_context_degrades_on_failure():
    assert fetch_product_context("", client=_FakeClient({})) is None
    assert fetch_product_context("155", client=_FakeClient({"success": False})) is None


import app.agent.product_context as pc
from app.multi_agent.orchestrator import MultiAgentOrchestrator


def test_build_messages_injects_current_product(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "fetch_product_context",
                        lambda item_id, client=None: "【当前咨询商品】名称：测试大衣" if item_id else None)
    set_current_item("155")
    agent = MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")
    engine = agent.engine
    engine.raw_messages = [{"role": "user", "content": "这是什么"}]
    msgs = engine._build_messages()
    assert any(m["role"] == "system" and "【当前咨询商品】" in m["content"] for m in msgs)
    set_current_item(None)
    msgs2 = engine._build_messages()
    assert not any("【当前咨询商品】" in m.get("content", "") for m in msgs2)
