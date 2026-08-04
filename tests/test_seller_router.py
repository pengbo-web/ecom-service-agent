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


def test_ambiguous_reply_mentioning_both_falls_back_to_read_only_default():
    """回复里 analyst/growth 两个词都出现时,不能凭 set 遍历顺序猜一个赢家。

    旧实现是 `for key in SELLER_AGENTS: if key in raw: return key`——SELLER_AGENTS
    是 set,`"growth" in raw` 和 `"analyst" in raw` 谁先判到全看哈希随机化的
    遍历顺序,在不同解释器进程里可能不同,导致暧昧回复不确定地路由到能产草稿的
    growth。为了不依赖"这次跑起来 growth 恰好排在前面"这种运气,不去构造一个
    刚好会暴露旧遍历顺序的例子,而是直接断言性质本身:即使回复同时命中两个
    候选词,结果也必须是只读的 SELLER_DEFAULT,不能是 growth。
    """
    r = SellerRouter(FakeClient("这个不好说,可能是 analyst 也可能是 growth"), "m")
    assert r.route("随便问问") == SELLER_DEFAULT


def test_ambiguous_routing_is_stable_across_key_order_permutations():
    """无论"先检查 analyst 还是先检查 growth",暧昧回复的结果都必须是默认值。

    非 flaky 的具体手段:真正的 `set` 遍历顺序由哈希随机化决定,没法在测试里
    可靠地摆布(两个字面量 `{"analyst","growth"}` vs `{"growth","analyst"}`
    在 CPython 里几乎总是产生相同的内部布局,并不能真的强出两种遍历顺序)。
    所以这里不去猜/摆布真·set 的哈希布局,而是把 `SELLER_AGENTS` 换成一个
    **保序的 list**,显式给出两种遍历顺序各跑一遍——`route()` 内部先把命中的
    候选收集进一个 `matched` 集合、只有长度为 1 才采用,不管 SELLER_AGENTS 本身
    以什么顺序被遍历,长度都是 2,恒定落兜底,所以两种顺序结果必须一致。

    这能验证"旧实现"确实有问题:旧代码是
    `for key in SELLER_AGENTS: if key in raw: return key`——直接换成按
    ["growth", "analyst"] 顺序遍历的 list 喂给它,会在遇到 "growth" 时立刻
    命中返回,而不会继续检查 "analyst";也就是说旧写法下这两种顺序至少有
    一种会把结果错误地返回成 "growth"(能产草稿的写侧),导致本测试失败。
    """
    from app.multi_agent import seller_router as sr_module

    reply = "analyst 和 growth 都提到了"
    original = sr_module.SELLER_AGENTS
    try:
        for ordered in (["analyst", "growth"], ["growth", "analyst"]):
            sr_module.SELLER_AGENTS = ordered
            r = SellerRouter(FakeClient(reply), "m")
            assert r.route("随便问问") == SELLER_DEFAULT, f"order={ordered}"
    finally:
        sr_module.SELLER_AGENTS = original


def test_reply_mentioning_exactly_one_name_routes_to_it():
    assert SellerRouter(FakeClient("growth"), "m").route("涨粉") == "growth"
    assert SellerRouter(FakeClient("analyst"), "m").route("看数据") == "analyst"


def test_history_is_trimmed_into_prompt():
    c = FakeClient("analyst")
    SellerRouter(c, "m").route("再看看", history=[
        {"role": "user", "content": "近7天GMV"},
        {"role": "assistant", "content": "12 万"},
    ])
    prompt = c.calls[0]["messages"][0]["content"]
    assert "近7天GMV" in prompt
