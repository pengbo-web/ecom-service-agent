"""ApeRAG 适配器:映射/容错/未配置短路。"""

import httpx

import app.agent.recall.external_kb as ext
from app.agent.recall.external_kb import aperag_search
from app.config.settings import settings


def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "aperag_api_key", "sk-test")
    monkeypatch.setattr(settings, "aperag_collection_id", "col_test")


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_maps_items_to_standard_rows(monkeypatch):
    _cfg(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp(200, {"items": [
            {"rank": 1, "score": 0.87, "content": "签收7天内可退",
             "source": "docs/退换货政策.md", "recall_type": "vector_search"},
        ]})

    monkeypatch.setattr(ext.httpx, "post", fake_post)
    rows = aperag_search("退货政策")
    assert rows == [{"doc": "退换货政策", "section": "vector_search",   # .md 已去后缀(来源标注观感)
                     "score": 0.87, "text": "签收7天内可退"}]
    assert "col_test/searches" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["rerank"] is False
    # similarity 为服务端必填(缺省整体500,实测教训),必须始终随载荷携带
    assert captured["json"]["vector_search"]["similarity"] == settings.aperag_min_similarity
    assert captured["timeout"] == settings.aperag_timeout_s   # ApeRAG 专属超时(冷启首查>6s,与embedding快速失败值分开)


def test_empty_items_is_no_hit_not_degrade(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(ext.httpx, "post",
                        lambda *a, **k: _Resp(200, {"items": []}))
    assert aperag_search("无关问题") == []          # [] 表示正常无命中


def test_non_dict_item_returns_none(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(ext.httpx, "post",
                        lambda *a, **k: _Resp(200, {"items": ["not-a-dict"]}))
    assert aperag_search("退货政策") is None


def test_http_error_returns_none(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(ext.httpx, "post",
                        lambda *a, **k: _Resp(500, text="boom"))
    assert aperag_search("退货政策") is None        # None 触发降级


def test_network_exception_returns_none(monkeypatch):
    _cfg(monkeypatch)

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ext.httpx, "post", boom)
    assert aperag_search("退货政策") is None


def test_missing_config_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "aperag_api_key", "")
    called = []
    monkeypatch.setattr(ext.httpx, "post", lambda *a, **k: called.append(1))
    assert aperag_search("退货政策") is None
    assert called == []                              # 未配置连请求都不发


# ---------------------------------------------------------------------------
# 任务①:全文路(fulltext_search)可配置,默认关闭
# ---------------------------------------------------------------------------

def test_fulltext_leg_omitted_by_default(monkeypatch):
    """默认 aperag_fulltext_enabled=False:payload 里不带 fulltext_search 键
    (不是带上一个空字典——服务端字段存在与否语义不同,必须整键不发)。"""
    _cfg(monkeypatch)
    assert settings.aperag_fulltext_enabled is False   # 锁定默认值本身
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return _Resp(200, {"items": []})

    monkeypatch.setattr(ext.httpx, "post", fake_post)
    aperag_search("退货政策")
    assert "fulltext_search" not in captured["json"]
    assert captured["json"]["vector_search"]["topk"] == settings.recall_kb_top_k


def test_fulltext_leg_included_when_enabled(monkeypatch):
    """显式打开开关(等 collection 配好中文分词后的场景):payload 带回
    fulltext_search,topk 与向量路一致——只翻开关,调用形状不用再改。"""
    _cfg(monkeypatch)
    monkeypatch.setattr(settings, "aperag_fulltext_enabled", True)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return _Resp(200, {"items": []})

    monkeypatch.setattr(ext.httpx, "post", fake_post)
    aperag_search("退货政策")
    assert captured["json"]["fulltext_search"] == {"topk": settings.recall_kb_top_k}


# ---------------------------------------------------------------------------
# 任务②:超时值 —— 默认值本身 + 超时确实按这个值传给 httpx,超时不致命
# ---------------------------------------------------------------------------

def test_default_timeout_is_3s():
    """锁定本次任务的超时选择(见 app/config/settings.py aperag_timeout_s 注释:
    owner 实测纯向量路 p90=1.11s/最坏=2.36s,3s≈2.7×p90 且仍高于实测最坏值)。
    这条测试的意义是防止今后有人不经讨论就把默认值悄悄改回旧的 10s。"""
    assert settings.aperag_timeout_s == 3.0


def test_timeout_exception_returns_none_not_raised(monkeypatch):
    """httpx 超时必须被当成"不可用"吞掉,而不是把异常炸给调用方——
    这是"超时不致命,该轮无注入继续生成"这条约束的最底层保证。"""
    _cfg(monkeypatch)

    def boom(*a, **k):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(ext.httpx, "post", boom)
    assert aperag_search("退货政策") is None


def test_timeout_value_passed_to_httpx_matches_setting(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(settings, "aperag_timeout_s", 3.0)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["timeout"] = timeout
        return _Resp(200, {"items": []})

    monkeypatch.setattr(ext.httpx, "post", fake_post)
    aperag_search("退货政策")
    assert captured["timeout"] == 3.0
