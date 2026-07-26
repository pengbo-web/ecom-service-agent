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
