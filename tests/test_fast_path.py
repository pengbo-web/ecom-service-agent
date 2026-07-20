from app.hardening.fast_path import match_fast_path


def test_greeting_hits():
    r = match_fast_path("你好")
    assert r and r["intent"] == "greeting"
    assert r["reply"]


def test_thanks_hits():
    assert match_fast_path("谢谢啦")["intent"] == "thanks"


def test_bye_hits():
    assert match_fast_path("再见")["intent"] == "bye"


def test_business_query_misses():
    assert match_fast_path("我的订单 ORD-20240115-001 发货了吗") is None
    assert match_fast_path("这件衣服质量有问题要退货") is None
