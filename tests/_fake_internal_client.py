"""把 `internal_http.internal_client` 打成假客户端的公共夹具。

**为什么需要它**:ApeRAG 的读写两个模块原先直接调 `httpx.post/get`,测试就打桩在
那个接缝上。后来这些调用改走 `internal_client(url)`(内网地址绕过系统代理——
不改的话 uvicorn 进程里每次调用都被代理吃掉,见 app/net/internal_http.py),
接缝随之从"模块级 httpx 函数"变成"上下文管理器里的 client 方法"。

打桩点必须跟着接缝走,而不是让生产代码为了迁就测试保留旧形状。这里提供一个
最小的假 client,让用例仍然只关心"发出去的请求长什么样、返回什么"。
"""

from __future__ import annotations

from contextlib import contextmanager


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class FakeClient:
    """记录调用参数的假 client。`handler(method, url, kwargs)` 返回 FakeResponse。"""

    def __init__(self, handler, record: list):
        self._handler = handler
        self._record = record

    def _call(self, method, url, **kwargs):
        self._record.append({"method": method, "url": url, **kwargs})
        return self._handler(method, url, kwargs)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


def patch_internal_client(monkeypatch, module, handler) -> list:
    """把 `module.internal_client` 换成产出 FakeClient 的上下文管理器。

    返回一个 list,里面按顺序记下每次请求的 method/url/kwargs(含 timeout),
    供用例断言"超时值确实透传了""fulltext 那条腿有没有发"这类事情。
    """
    record: list = []

    @contextmanager
    def _fake(url, timeout=None, **kwargs):
        record.append({"__client__": True, "url": url, "timeout": timeout})
        yield FakeClient(handler, record)

    monkeypatch.setattr(module, "internal_client", _fake)
    return record
