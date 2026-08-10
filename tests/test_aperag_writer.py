"""ApeRAG 写入客户端(V1)。

**这份测试证明不了写入面真的通。** 它 mock 掉 httpx,验证的是"按我以为的形状
发请求 + 各种失败姿态正确",而真正会出错的恰恰是"我以为的形状"和服务端契约
不一致。那件事只有 `scripts/aperag_write_smoke.py` 对着真服务跑才能验证——
这也是方案里 V1 的验收标准。

所以这里钉的是**行为契约**:失败不抛、幂等覆盖、确认失败要如实报错。

打桩点是 `w.internal_client` 而不是 `w.httpx.post`:这些调用改走内网客户端了
(内网地址绕过系统代理——不改的话 uvicorn 进程里每次调用都被代理吃掉,而
fail-soft 会把它表现成"知识库是空的")。**打桩点跟着接缝走**,不让生产代码为了
迁就测试保留旧形状。
"""

from __future__ import annotations

import pytest

from app.config.settings import settings
from app.knowledge import aperag_writer as w


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


def _patch_post(monkeypatch, post_fn):
    """把 w.internal_client 换掉,POST 转给 `post_fn(url, **kwargs)`。"""
    from tests._fake_internal_client import patch_internal_client

    return patch_internal_client(
        monkeypatch, w, lambda method, url, kwargs: post_fn(url, **kwargs))


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "aperag_api_key", "test-key")
    monkeypatch.setattr(settings, "aperag_base_url", "http://ape.test")


# ---------- upload ----------

def test_upload_returns_document_id(monkeypatch):
    seen = {}

    def _post(url, **kw):
        seen["url"] = url
        seen["files"] = kw.get("files")
        return _Resp(200, {"document_id": "doc-1", "filename": "a.md",
                           "status": "UPLOADED"})

    _patch_post(monkeypatch, _post)
    assert w.upload_document("col-x", "a.md", "内容") == "doc-1"
    assert seen["url"].endswith("/collections/col-x/documents/upload")
    # multipart 的 part 名必须是 `file`(服务端契约),内容按 UTF-8 编码
    name, payload, ctype = seen["files"]["file"]
    assert name == "a.md" and payload == "内容".encode("utf-8")


def test_upload_non_200_returns_none(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _Resp(413, text="too large"))
    assert w.upload_document("col-x", "a.md", "内容") is None


def test_upload_missing_document_id_returns_none(monkeypatch):
    """响应 200 但没带 document_id:不能当成功——后续 confirm 会拿着 None 去调。"""
    _patch_post(monkeypatch, lambda *a, **k: _Resp(200, {"filename": "a.md"}))
    assert w.upload_document("col-x", "a.md", "内容") is None


def test_upload_network_error_does_not_raise(monkeypatch):
    """离线写入失败下一轮会重来;让异常穿透只会打断整条提炼流程,
    前面花掉的 LLM 成本就白费了。"""
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    _patch_post(monkeypatch, _boom)
    assert w.upload_document("col-x", "a.md", "内容") is None


def test_no_api_key_skips_quietly(monkeypatch):
    monkeypatch.setattr(settings, "aperag_api_key", "")
    assert w.upload_document("col-x", "a.md", "内容") is None
    assert w.confirm_documents("col-x", ["d1"]) == 0
    assert w.list_documents("col-x") is None
    assert w.delete_document("col-x", "d1") is False


# ---------- confirm ----------

def test_confirm_returns_count(monkeypatch):
    seen = {}

    def _post(url, **kw):
        seen["json"] = kw.get("json")
        return _Resp(200, {"confirmed_count": 2, "failed_count": 0})

    _patch_post(monkeypatch, _post)
    assert w.confirm_documents("col-x", ["d1", "d2"]) == 2
    assert seen["json"] == {"document_ids": ["d1", "d2"]}


def test_confirm_reports_partial_success(monkeypatch):
    """批量确认可能部分成功,返回条数而不是布尔——调用方要能如实记账。"""
    _patch_post(monkeypatch, lambda *a, **k: _Resp(200, {"confirmed_count": 1,
                                                    "failed_count": 1}))
    assert w.confirm_documents("col-x", ["d1", "d2"]) == 1


def test_confirm_empty_list_is_noop(monkeypatch):
    def _must_not_call(*a, **k):
        raise AssertionError("空列表不该发请求")

    _patch_post(monkeypatch, _must_not_call)
    assert w.confirm_documents("col-x", []) == 0


# ---------- put_document(幂等覆盖) ----------

def test_put_deletes_old_version_before_writing(monkeypatch):
    """同名文档先删后写。

    提炼是周期性重跑的:同一簇对话下个月还会被重新提炼。不覆盖的话,向量库里
    会堆满同一份经验的十几个版本,检索时互相挤占 top-k,而且旧版本的过时结论
    会和新版本一起被召回。
    """
    calls = []
    monkeypatch.setattr(w, "list_documents",
                        lambda cid: [{"id": "old-1", "name": "exp.md"},
                                     {"id": "other", "name": "别的.md"}])
    monkeypatch.setattr(w, "delete_document",
                        lambda cid, did: calls.append(("del", did)) or True)
    monkeypatch.setattr(w, "upload_document",
                        lambda cid, fn, c: calls.append(("up", fn)) or "new-1")
    monkeypatch.setattr(w, "confirm_documents",
                        lambda cid, ids: calls.append(("confirm", tuple(ids))) or 1)

    assert w.put_document("col-x", "exp.md", "新内容") == "new-1"
    assert calls == [("del", "old-1"), ("up", "exp.md"), ("confirm", ("new-1",))]


def test_put_still_writes_when_delete_fails(monkeypatch):
    """删不掉旧的**不阻断**写入:宁可短暂出现两份(下一轮会再清一次),
    也不能让新经验进不去——旧的至少还是有效知识,写入失败等于这轮提炼全白做。"""
    monkeypatch.setattr(w, "list_documents", lambda cid: [{"id": "old", "name": "e.md"}])
    monkeypatch.setattr(w, "delete_document", lambda cid, did: False)
    monkeypatch.setattr(w, "upload_document", lambda cid, fn, c: "new")
    monkeypatch.setattr(w, "confirm_documents", lambda cid, ids: 1)
    assert w.put_document("col-x", "e.md", "内容") == "new"


def test_put_skips_dedupe_when_listing_fails(monkeypatch):
    """列不出已有文档时跳过去重直接写,不因为读不到清单就整个写不进去。"""
    monkeypatch.setattr(w, "list_documents", lambda cid: None)
    monkeypatch.setattr(w, "delete_document",
                        lambda cid, did: (_ for _ in ()).throw(
                            AssertionError("列不出清单时不该删任何东西")))
    monkeypatch.setattr(w, "upload_document", lambda cid, fn, c: "new")
    monkeypatch.setattr(w, "confirm_documents", lambda cid, ids: 1)
    assert w.put_document("col-x", "e.md", "内容") == "new"


def test_put_returns_none_when_confirm_fails(monkeypatch):
    """上传成功但确认失败 = 文档停在 UPLOADED,**永远不会被检索到**。
    必须按写入失败处理,不能返回一个看起来成功的 id。"""
    monkeypatch.setattr(w, "list_documents", lambda cid: [])
    monkeypatch.setattr(w, "upload_document", lambda cid, fn, c: "new")
    monkeypatch.setattr(w, "confirm_documents", lambda cid, ids: 0)
    assert w.put_document("col-x", "e.md", "内容") is None


# ---------- 与检索侧的隔离 ----------

def test_writer_never_defaults_to_the_policy_collection(monkeypatch):
    """每个函数都显式收 collection_id,不复用 settings.aperag_collection_id。

    那个 id 是**政策知识库**的。经验/教训/洞察必须落在不同 collection——
    因为 ApeRAG 的检索请求没有元数据过滤,只能靠 collection 隔离
    (见 aperag_writer 模块顶部约束 3)。混进政策库意味着买家问退货政策时
    可能召回一段"历史成功话术",而那不是政策。
    """
    import ast
    import inspect

    for fn in (w.upload_document, w.confirm_documents, w.list_documents,
               w.delete_document, w.put_document):
        params = list(inspect.signature(fn).parameters)
        assert params[0] == "collection_id", fn.__name__

    # 用 AST 而不是文本搜:模块顶部的说明文字里就写着这个名字("那个 id 是
    # 政策知识库的,别混")——按文本搜会把这句解释本身判成违规。只看真实的
    # 属性访问节点。
    tree = ast.parse(inspect.getsource(w))
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Attribute) and n.attr == "aperag_collection_id"]
    assert not bad, f"写入侧不该回落政策知识库的 collection(行 {bad})"
