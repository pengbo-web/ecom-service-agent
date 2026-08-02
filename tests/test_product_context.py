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
