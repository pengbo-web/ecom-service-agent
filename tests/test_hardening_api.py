from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.hardening.auth import make_admin_auth


def _app(token):
    app = FastAPI()
    auth = make_admin_auth(token)

    @app.get("/admin", dependencies=[Depends(auth)])
    def admin():
        return {"ok": True}

    return TestClient(app)


def test_no_token_allows_all():
    c = _app("")
    assert c.get("/admin").status_code == 200


def test_token_required_when_set():
    c = _app("secret")
    assert c.get("/admin").status_code == 401
    assert c.get("/admin", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.get("/admin", headers={"X-Admin-Token": "secret"}).status_code == 200


# ---------- 集成（Task 5 接入后通过）----------
from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.observability.store import TraceStore
from app.schemas.response import CustomerServiceResponse, IntentType


class _FakeAgent:
    def __init__(self, session_path=None):
        self.event_sink = None; self.client = None; self.raw_messages = []
        self.chat_called = False

    def chat(self, user_input):
        self.chat_called = True
        return CustomerServiceResponse(intent=IntentType.ORDER_QUERY, confidence=0.9,
            reply="已发货", requires_human=False, follow_up_question=None)


def _client(tmp_path, **over):
    store = TraceStore(str(tmp_path / "tr.db")); store.init_schema()
    mgr = SessionManager(agent_factory=lambda p, u=None: _FakeAgent(p))
    app = create_app(session_manager=mgr, trace_store=store, hitl=None, **over)
    return TestClient(app)


def test_fast_path_skips_agent(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/chat", json={"session_id": "s1", "message": "你好"})
    assert "小夕" in r.text            # 规则秒回
    assert "metadata" not in r.text    # 未走 Agent


def test_admin_endpoint_guarded(tmp_path):
    c = _client(tmp_path, admin_token="secret")
    assert c.get("/api/metrics").status_code == 401
    assert c.get("/api/metrics", headers={"X-Admin-Token": "secret"}).status_code == 200


def test_chat_open_without_admin_token(tmp_path):
    c = _client(tmp_path, admin_token="secret")
    r = c.post("/api/chat", json={"session_id": "s1", "message": "查订单"})
    assert r.status_code == 200


# ---------- 阶段一 gap④:四道门必须补一条命名 trace ----------

from contextlib import contextmanager


class _FakeTraceRoot:
    def __init__(self):
        self.updates = []

    def update(self, **kw):
        self.updates.append(kw)


def _fake_background_trace(calls):
    @contextmanager
    def _bt(name, session_id=None, user_id=None, input=None):
        root = _FakeTraceRoot()
        calls.append({"name": name, "session_id": session_id, "input": input, "root": root})
        yield root
    return _bt


def test_rate_limit_gate_produces_named_trace(tmp_path, monkeypatch):
    """命中限流门时,必须补一条名为 gate:rate_limit 的 trace,而不是像此前
    那样在 run_agent_streaming 之前就悄悄短路、完全不留痕迹。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "rate_limit_per_min", 0)   # 第一次就命中限流
    calls: list = []
    monkeypatch.setattr("app.observability.langfuse_bridge.background_trace",
                        _fake_background_trace(calls))

    c = _client(tmp_path)
    r = c.post("/api/chat", json={"session_id": "s1", "message": "你好"})
    assert r.status_code == 200
    assert "太快" in r.text
    assert len(calls) == 1
    assert calls[0]["name"] == "gate:rate_limit"
    assert calls[0]["root"].updates[0]["metadata"] == {"gate": "rate_limit"}


def test_manual_takeover_gate_produces_named_trace(tmp_path, monkeypatch):
    from app.hitl.manual_mode import ManualMode

    class _FakeHitl:
        def __init__(self):
            self.manual_mode = ManualMode()

    hitl = _FakeHitl()
    hitl.manual_mode.enter("s-manual")
    calls: list = []
    monkeypatch.setattr("app.observability.langfuse_bridge.background_trace",
                        _fake_background_trace(calls))

    store = TraceStore(str(tmp_path / "tr.db")); store.init_schema()
    mgr = SessionManager(agent_factory=lambda p, u=None: _FakeAgent(p))
    app = create_app(session_manager=mgr, trace_store=store, hitl=hitl)
    c = TestClient(app)
    r = c.post("/api/chat", json={"session_id": "s-manual", "message": "你好"})
    assert r.status_code == 200
    assert "人工客服" in r.text
    assert len(calls) == 1
    assert calls[0]["name"] == "gate:manual_takeover"


def test_fast_path_gate_produces_named_trace(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.observability.langfuse_bridge.background_trace",
                        _fake_background_trace(calls))

    c = _client(tmp_path)
    r = c.post("/api/chat", json={"session_id": "s1", "message": "你好"})
    assert r.status_code == 200
    assert "小夕" in r.text
    assert len(calls) == 1
    assert calls[0]["name"] == "gate:fast_path"


def test_cost_ceiling_gate_produces_named_trace(tmp_path, monkeypatch):
    from app.config import settings as st
    # 关掉快路径,否则"你好"会被规则秒回,永远走不到成本闸
    monkeypatch.setattr(st.settings, "fast_path_enabled", False)
    monkeypatch.setattr(st.settings, "daily_request_budget", 0)
    calls: list = []
    monkeypatch.setattr("app.observability.langfuse_bridge.background_trace",
                        _fake_background_trace(calls))

    c = _client(tmp_path)
    r = c.post("/api/chat", json={"session_id": "s1", "message": "帮我查一下订单状态"})
    assert r.status_code == 200
    assert "上限" in r.text
    assert len(calls) == 1
    assert calls[0]["name"] == "gate:cost_ceiling"


def test_gates_unaffected_when_langfuse_disabled(tmp_path):
    """门控关(默认态):四道门的短路回复必须与开关无关,行为不变。"""
    from app.config import settings as st
    assert st.settings.langfuse_enabled is False
    c = _client(tmp_path)
    r = c.post("/api/chat", json={"session_id": "s1", "message": "你好"})
    assert r.status_code == 200
    assert "小夕" in r.text


def test_gate_degrades_silently_when_langfuse_init_raises(tmp_path, monkeypatch):
    """核心 fail-soft 性质:门控开着但 Langfuse 初始化本身抛异常——命中的
    那道门的短路回复必须与不接 Langfuse 时完全一致,不能因观测层出错而
    影响这一轮请求。"""
    import app.observability.langfuse_bridge as bridge_mod
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "langfuse_enabled", True)
    monkeypatch.setattr(bridge_mod, "_ensure_env",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    c = _client(tmp_path)
    r = c.post("/api/chat", json={"session_id": "s1", "message": "你好"})
    assert r.status_code == 200
    assert "小夕" in r.text
