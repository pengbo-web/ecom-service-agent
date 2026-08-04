"""卖家域路由:两个合法值、非法输出兜底、历史裁剪。"""

from types import SimpleNamespace

from app.multi_agent.seller_router import SellerRouter, SELLER_DEFAULT, SELLER_AGENTS


class FakeClient:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self._reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_valid_values():
    assert SELLER_AGENTS == {"analyst", "growth"}
    assert SELLER_DEFAULT == "analyst"


def test_routes_to_growth():
    r = SellerRouter(FakeClient("growth"), "m")
    assert r.route("有哪些下单没付款的?") == "growth"


def test_routes_to_analyst():
    r = SellerRouter(FakeClient("analyst"), "m")
    assert r.route("这周退款率怎么样") == "analyst"


def test_garbage_falls_back_to_default():
    """模型输出跑偏时必须落到只读的 analyst,而不是能产草稿的 growth。"""
    r = SellerRouter(FakeClient("我觉得应该是……"), "m")
    assert r.route("随便说点什么") == SELLER_DEFAULT


def test_empty_content_falls_back():
    r = SellerRouter(FakeClient(None), "m")
    assert r.route("x") == SELLER_DEFAULT


def test_history_is_trimmed_into_prompt():
    c = FakeClient("analyst")
    SellerRouter(c, "m").route("再看看", history=[
        {"role": "user", "content": "近7天GMV"},
        {"role": "assistant", "content": "12 万"},
    ])
    prompt = c.calls[0]["messages"][0]["content"]
    assert "近7天GMV" in prompt
