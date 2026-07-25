"""查询改写:多轮补全指代;首问/关闭/失败/空输出一律回退原句。"""

from types import SimpleNamespace

from app.agent.recall.rewrite import rewrite_for_recall
from app.config.settings import settings


def _client(reply=None, raises=False, capture=None):
    def create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if raises:
            raise RuntimeError("llm down")
        msg = SimpleNamespace(content=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _msgs(*users):
    out = []
    for u in users:
        out.append({"role": "user", "content": u})
        out.append({"role": "assistant", "content": "好的"})
    return out


def test_multi_turn_rewrites(monkeypatch):
    cap = {}
    c = _client(reply="退货运费谁承担", capture=cap)
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么", "那运费呢?"), "那运费呢?")
    assert got == "退货运费谁承担"
    sent = str(cap["messages"])
    assert "退货政策是什么" in sent and "那运费呢?" in sent   # 历史与原句都送给了改写


def test_first_turn_skips_llm():
    c = _client(raises=True)                                  # 若真调 LLM 会抛
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么"), "退货政策是什么")
    assert got == "退货政策是什么"


def test_disabled_returns_raw(monkeypatch):
    monkeypatch.setattr(settings, "recall_rewrite_enabled", False)
    c = _client(raises=True)
    got = rewrite_for_recall(c, "m", _msgs("a", "b"), "b")
    assert got == "b"


def test_llm_error_returns_raw():
    c = _client(raises=True)
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么", "那运费呢?"), "那运费呢?")
    assert got == "那运费呢?"


def test_empty_output_returns_raw():
    c = _client(reply="   ")
    got = rewrite_for_recall(c, "m", _msgs("退货政策是什么", "那运费呢?"), "那运费呢?")
    assert got == "那运费呢?"


def test_none_query_passthrough():
    c = _client(raises=True)
    assert rewrite_for_recall(c, "m", [], None) is None
