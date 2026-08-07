"""卖家会话 API:鉴权、开关、会话隔离、路由结果透出。"""

import pytest
from fastapi.testclient import TestClient

from app.multi_agent.agents import SELLER_AGENT_CONFIGS
from app.schemas.response import CustomerServiceResponse, IntentType


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    # 隔离数据库:get_db() 默认是进程内单例、指向真实的 app/sessions/ecom.db。
    # 本文件的 overview 端点会读 turn_signals/orders 等表,不隔离的话断言会被
    # "开发机上真实跑过的会话"污染(N2 情绪分布那条就是这样翻的车:线上
    # 真实用过之后 turn_signals 里有数据,"空库应为 0" 的断言在同一台机器上
    # 就不成立了)。做法与 tests/test_growth_api.py 一致:每个测试各自一份
    # tmp_path 下的临时 sqlite 文件,测试结束后把单例复位。
    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "seller_api_test.db"))
    db.init_schema()
    set_db(db)

    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


# 管理鉴权走 X-Admin-Token(见 app/hardening/auth.py::make_admin_auth),不是
# Authorization: Bearer —— 与 tests/test_skill_admin_api.py 等既有 admin 端点
# 测试用的头一致。
AUTH = {"X-Admin-Token": "T"}


def test_seller_chat_requires_auth(client):
    r = client.post("/api/seller/chat", json={"session_id": "s1", "message": "近7天GMV"})
    assert r.status_code in (401, 403)


def test_seller_overview_requires_auth(client):
    assert client.get("/api/seller/overview").status_code in (401, 403)


def test_seller_overview_returns_metrics_and_anomalies(client):
    r = client.get("/api/seller/overview?window_days=7", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "overview" in body and "anomalies" in body
    assert body["overview"]["success"] is True


def test_seller_overview_carries_emotion_section(client):
    """N2 回归:情绪分布必须真正跨过 HTTP 边界到达控制台,不能只是
    service_quality() 这个 Python 函数自己测过、端点却没转发。

    这里改为**灌入一组已知的 turn_signals 再断言端点报出这组数据**,而不是
    断言"空库时 total 应为 0"——后者只在没人真正用过这台机器时才成立,
    是关于开发者本机历史的断言,不是关于端点行为的断言(client 隔离前,
    这条测试在跑过真实对话的机器上会读到 ecom.db 里的真实行而失败,报的
    却是 "assert 2 == 0" 这种看似端点坏了的假象)。灌入已知数据后端点必须
    原样报出这组数据,才真正证明了"确实转发了 service_quality() 算出来的
    情绪分布",而不仅仅是"quality.emotion 这个键存在"。
    """
    from app.db import get_db
    db = get_db()
    for emotion, level in (("neutral", 0), ("neutral", 0), ("unhappy", 2), ("angry", 3)):
        db.record_turn_signal("s", "u", "其他", emotion, level, False)

    r = client.get("/api/seller/overview?window_days=7", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "quality" in body
    emotion = body["quality"]["emotion"]
    assert emotion["total"] == 4
    assert emotion["counts"] == {"neutral": 2, "unhappy": 1, "angry": 1}
    assert emotion["angry_rate"] == pytest.approx(0.25)
    # 既有键必须原样还在——新增是附加的,不是替换
    assert "overview" in body and "products" in body and "anomalies" in body


def test_seller_overview_emotion_section_is_zero_on_empty_db(client):
    """空库场景本身仍值得测:除零必须返回 0.0 而不是 None(消费方是 LLM,
    None 会被渲染成 'null' 诱导模型现编数字)。这条在隔离后的临时库上
    才是关于端点行为的断言,而不是关于开发者本机历史的断言。"""
    r = client.get("/api/seller/overview?window_days=7", headers=AUTH)
    assert r.status_code == 200
    emotion = r.json()["quality"]["emotion"]
    assert emotion["total"] == 0
    assert emotion["angry_rate"] == 0.0
    assert set(emotion["counts"]) == {"neutral", "unhappy", "angry"}


def test_seller_chat_returns_agent_key(client, monkeypatch):
    """本轮由哪个画像作答,必须**读编排器的真实结果**,不能是端点兜底猜的。

    这里刻意用 growth 而不是 analyst:analyst 恰好是端点取不到属性时的兜底值,
    用它做断言就分不清"真读到了"还是"兜底成了同一个值"——测试会失去可失败性。

    FakeOrch.chat() 刻意返回**真实的 CustomerServiceResponse**(与
    SellerOrchestrator.chat() 的真实契约一致),不用裸 dict——用 dict 会掩盖
    "端点按属性访问读 reply"这件事:dict 恰好也有 `.get("reply")` 能用,
    测试会在端点仍是 `isinstance(result, dict)` 分支时假装通过。
    """
    from app.api import app as appmod

    class FakeOrch:
        last_agent_key = "growth"
        def chat(self, text):
            return CustomerServiceResponse(
                intent=IntentType.OTHER, confidence=1.0,
                reply=f"收到:{text}", requires_human=False)
        def save(self):
            pass

    monkeypatch.setattr(appmod.seller_sessions, "get_or_create",
                        lambda sid, user_id=None: FakeOrch())
    r = client.post("/api/seller/chat",
                    json={"session_id": "s1", "message": "有哪些下单没推进的?"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["agent_key"] == "growth"
    assert body["agent"] == SELLER_AGENT_CONFIGS["growth"]["name"]
    assert "收到" in body["reply"]


def test_seller_chat_reply_is_not_stringified_response_object(client, monkeypatch):
    """核心回归:orch.chat() 返回**真实的 CustomerServiceResponse pydantic 对象**
    (SellerOrchestrator.chat() 的真实契约,不是 dict、不是临时凑的桩),端点必须
    把它的 `.reply` 字段原样透出,而不是把整个对象 `str()` 成一坨 repr 发给店主。

    这与买家侧 streaming.py 的 `result.reply` 读法必须一致——同一个类,同一种
    读法。falsify 时把端点临时改回
    `result.get("reply", "") if isinstance(result, dict) else str(result)`
    应能让本测试失败(reply 会变成 "intent=<IntentType...> confidence=1.0 reply=..."
    这种 repr 串)。
    """
    from app.api import app as appmod

    real_reply = "结论：近 7 天退款率高达 26.1%，建议核实商品详情页描述是否与实物一致。"

    class RealOrch:
        last_agent_key = "analyst"
        def chat(self, text):
            return CustomerServiceResponse(
                intent=IntentType.PRODUCT_CONSULT, confidence=1.0,
                reply=real_reply, requires_human=False, follow_up_question="")
        def save(self):
            pass

    monkeypatch.setattr(appmod.seller_sessions, "get_or_create",
                        lambda sid, user_id=None: RealOrch())
    r = client.post("/api/seller/chat",
                    json={"session_id": "s1", "message": "P001 退款率怎么样"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == real_reply
    for scaffold in ("intent=", "confidence=", "requires_human=", "IntentType"):
        assert scaffold not in body["reply"], (
            f"回复里混入了 CustomerServiceResponse 的 repr 片段 {scaffold!r}: {body['reply']!r}")


def test_seller_chat_404_when_console_disabled(client, monkeypatch):
    """控制台关掉时聊天端点也必须是 404(此前只测了 overview 那一半)。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "seller_console_enabled", False)
    r = client.post("/api/seller/chat",
                    json={"session_id": "s1", "message": "x"}, headers=AUTH)
    assert r.status_code == 404


def test_seller_sessions_are_isolated_from_buyer_sessions(client):
    """店主和买家用同一个 session_id 也不能串——两套 SessionManager 各自独立。

    买家侧的 SessionManager 是 create_app() 内部的局部变量(未注入时新建、
    可注入以便测试隔离),不是模块级名字,因此不能直接
    `from app.api.app import sessions` 拿到它——真实拿法是从这条 client 背后
    那个具体的 app 实例上取(create_app() 里已把它挂到 app.state.session_manager,
    专门供这类内省使用)。这里比对的正是"这次请求实际会用到的买家会话管理器"
    与卖家会话管理器,而不是凭空构造的另一个实例。
    """
    from app.api.app import seller_sessions
    buyer_sessions = client.app.state.session_manager
    assert seller_sessions is not buyer_sessions
    assert seller_sessions._base_dir != buyer_sessions._base_dir


def test_disabled_console_returns_404(client, monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "seller_console_enabled", False)
    r = client.get("/api/seller/overview", headers=AUTH)
    assert r.status_code == 404
