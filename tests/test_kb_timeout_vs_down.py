"""知识库"超时"与"连不上"必须分开:两者的运维动作完全不同。

**实测缺陷**(走查知识库写入侧时抓到)。上传一篇文档之后的一段窗口里,
`/api/admin/kb/documents` **连续 30 秒超时失败**,界面提示:

    知识库服务连不上，请确认 ApeRAG 已启动

而 ApeRAG **当时是活着的**——同一时段直连它的文档列表接口 **0.5 秒返回 200**。
它只是在忙着建刚上传那篇的索引。

这句提示会**把人指去重启一个健康的服务**,而正确的动作是等几十秒再刷新。根因是
`aperag_writer.list_documents` 用一个 `except Exception` 把超时与连接失败塌成同一个
`None`,调用方无从区分。

这是这一整轮反复出现的同一个模式的又一次:**把"暂时办不到"当成"办不到"**,
而且给出的提示指向了错误的动作。
"""

import httpx
import pytest

from app.knowledge import aperag_writer as w


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    """把 ApeRAG 配置补齐,否则 `_ready()` 早退,测不到下面的分支。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "aperag_api_key", "k")
    monkeypatch.setattr(st.settings, "aperag_base_url", "http://127.0.0.1:8100")
    monkeypatch.setattr(st.settings, "aperag_write_timeout_s", 30.0)


def _fake_client(exc=None, status=200, payload=None):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            if exc is not None:
                raise exc
            class R:
                status_code = status
                text = "boom"
                @staticmethod
                def json(): return payload or {"items": []}
            return R()
    return lambda *a, **kw: C()


# --------------------------------------------------------------------------
# 区分本身
# --------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("timed out"),
    httpx.ConnectTimeout("timed out"),
    httpx.WriteTimeout("timed out"),
    httpx.PoolTimeout("timed out"),
])
def test_timeout_raises_not_returns_none(monkeypatch, exc):
    """httpx 的超时分好几种,全都算超时——它们的共同点是"请求发出去了或建连中,
    但没在时限内拿到结果",与"压根连不上"在运维动作上完全不同。"""
    monkeypatch.setattr(w, "internal_client", _fake_client(exc=exc))
    with pytest.raises(w.KnowledgeBaseTimeout):
        w.list_documents("col-1")


def test_connect_error_still_returns_none(monkeypatch):
    """连不上仍走既有的 None 出口:调用方 `if docs is None` 的判断不能被改坏。"""
    monkeypatch.setattr(w, "internal_client",
                        _fake_client(exc=httpx.ConnectError("refused")))
    assert w.list_documents("col-1") is None


def test_http_error_still_returns_none(monkeypatch):
    """非 200 也是既有的 None 出口,不该被误升级成超时。"""
    monkeypatch.setattr(w, "internal_client", _fake_client(status=500))
    assert w.list_documents("col-1") is None


def test_success_unchanged(monkeypatch):
    monkeypatch.setattr(w, "internal_client",
                        _fake_client(payload={"items": [{"id": "d1"}]}))
    assert w.list_documents("col-1") == [{"id": "d1"}]


def test_not_ready_still_returns_none(monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "aperag_api_key", "")
    assert w.list_documents("col-1") is None


# --------------------------------------------------------------------------
# 端点给出的提示必须指向正确的动作
# --------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")
    monkeypatch.setattr(st.settings, "db_path", str(tmp_path / "ecom.db"))
    monkeypatch.setattr(st.settings, "aperag_api_key", "k")
    monkeypatch.setattr(st.settings, "aperag_collection_id", "col-1")
    from app.api.app import create_app
    return TestClient(create_app())


AUTH = {"X-Admin-Token": "T"}


def test_endpoint_says_wait_not_restart_on_timeout(client, monkeypatch):
    """核心断言:超时时**不能**让人去重启一个健康的服务。"""
    monkeypatch.setattr(w, "list_documents",
                        lambda cid: (_ for _ in ()).throw(w.KnowledgeBaseTimeout("t")))
    r = client.get("/api/admin/kb/documents", headers=AUTH)
    assert r.status_code == 503, "暂时不可用应是 503,不是 502(上游坏了)"
    detail = r.json().get("detail", "")
    assert "超时" in detail
    assert "建索引" in detail, "要说出最常见的原因,否则人还是不知道该等什么"
    assert "确认 ApeRAG 已启动" not in detail, (
        "这句会把人指去重启一个健康的服务——正是这次要修的那个错")


def test_endpoint_still_says_down_when_really_down(client, monkeypatch):
    """真连不上时仍要说"连不上":这次改动不能把两种情况反过来混。"""
    monkeypatch.setattr(w, "list_documents", lambda cid: None)
    r = client.get("/api/admin/kb/documents", headers=AUTH)
    assert r.status_code == 502
    assert "连不上" in r.json().get("detail", "")


def test_endpoint_ok_path_unchanged(client, monkeypatch):
    monkeypatch.setattr(w, "list_documents",
                        lambda cid: [{"id": "d1", "name": "a.md",
                                      "vector_index_status": "ACTIVE"}])
    d = client.get("/api/admin/kb/documents", headers=AUTH).json()
    assert d["success"] is True
    assert d["documents"][0]["vector_index_status"] == "ACTIVE"
