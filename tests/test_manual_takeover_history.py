"""人工接管期间买家说的话必须进历史。

**实测缺陷**(走查人工接管时抓到)。走完整闭环:AI 正常回 → 坐席切人工 → 买家再发
一句 → Agent 正确停口回「🎧 当前会话已转由人工客服处理」→ 坐席人工回复 → 切回自动。
功能全对,但事后看买家历史:

    user       你好
    assistant  您好，我是并夕夕智能客服小夕…
    assistant  您好，我是人工客服小李，已接手您的问题。      ← 坐席那句在
    user       好的谢谢
    assistant  不客气～…

**接管期间买家说的「那我的订单呢」和那句 gate 回复都不在。**

对比很清楚:隔壁那道快路径门的注释明写着——"仍把这轮问答写进会话历史并落盘,
**保证刷新/切换后可回显**(不因走快路径而丢失)",而且它确实 append + save 了。
只有接管这条路漏了。同一条纪律,隔壁做了,这条没做。

后果按疼的程度排:

1. **坐席在工作台打开会话,看不到买家在等待期间说了什么**——而接管正是为了处理
   买家在说的事;
2. 买家刷新页面,自己刚说的话不见了;
3. 审计与复盘缺这一段。
"""

import json

import pytest
from fastapi.testclient import TestClient

AUTH = {"X-Admin-Token": "T"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    monkeypatch.setattr(st.settings, "hitl_db_path", str(tmp_path / "hitl.db"))

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "ecom.db"))
    db.init_schema()
    set_db(db)

    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


def _history(client, sid):
    r = client.get(f"/api/session/{sid}/history")
    if r.status_code != 200:
        return []
    d = r.json()
    return d.get("messages") or d.get("turns") or (d if isinstance(d, list) else [])


def _start_conversation(client):
    """建一个真实会话(接管那道门刻意跑在 ensure_active 之前,只能往已有会话上追加)。"""
    from app.db import get_db
    from app.api.conversations import ensure_active
    sid, _ = ensure_active(get_db(), "", "u1")
    return sid


def test_buyer_message_during_takeover_is_persisted(client):
    """核心断言:接管期间买家说的话必须进历史。"""
    sid = _start_conversation(client)
    client.post(f"/api/session/{sid}/takeover", headers=AUTH)

    r = client.post("/api/chat", json={"message": "那我的订单呢",
                                       "session_id": sid, "user_id": "u1"})
    assert r.status_code == 200
    assert "人工客服处理" in r.text, "接管门没生效,这条测试的前提就不成立"

    msgs = _history(client, sid)
    users = [m for m in msgs if m.get("role") == "user"]
    assert any("那我的订单呢" in (m.get("content") or "") for m in users), (
        "接管期间买家说的话没进历史——坐席在工作台看不到他在问什么")


def test_gate_notice_is_persisted_too(client):
    """那句提示也要记:否则历史里买家的话后面没有任何回应,像是系统没理他。"""
    sid = _start_conversation(client)
    client.post(f"/api/session/{sid}/takeover", headers=AUTH)
    client.post("/api/chat", json={"message": "在吗", "session_id": sid, "user_id": "u1"})

    msgs = _history(client, sid)
    blob = json.dumps(msgs, ensure_ascii=False)
    assert "人工客服处理" in blob


def test_notice_text_is_a_single_source(client):
    """回给买家的那句 和 写进历史的那句必须是**同一个**常量。

    两处各写一遍字面量,改文案时漏掉一处,买家看到的和历史里记下的就会对不上
    ——而那种不一致在复盘时最难解释:坐席会以为系统当时说了另一句话。
    """
    from app.api.app import MANUAL_TAKEOVER_NOTICE
    sid = _start_conversation(client)
    client.post(f"/api/session/{sid}/takeover", headers=AUTH)
    r = client.post("/api/chat", json={"message": "在吗", "session_id": sid, "user_id": "u1"})

    assert MANUAL_TAKEOVER_NOTICE in r.text
    assert MANUAL_TAKEOVER_NOTICE in json.dumps(_history(client, sid), ensure_ascii=False)


def test_takeover_still_blocks_the_agent(client, monkeypatch):
    """记历史不能把接管本身削弱:Agent 绝不能在接管期间抢答。"""
    sid = _start_conversation(client)
    client.post(f"/api/session/{sid}/takeover", headers=AUTH)

    called = []
    import app.api.streaming as st
    monkeypatch.setattr(st, "run_agent_streaming",
                        lambda *a, **kw: called.append(1) or iter(()))

    client.post("/api/chat", json={"message": "查订单", "session_id": sid, "user_id": "u1"})
    assert called == [], "接管期间 Agent 被调用了——这道门的全部意义就是不让它开口"


def test_unknown_session_does_not_crash_the_gate(client):
    """会话不存在时(接管门跑在 ensure_active 之前)只跳过持久化,门本身照常生效
    ——绝不能因为记历史失败而让接管失效。"""
    client.post("/api/session/ghost-session/takeover", headers=AUTH)
    r = client.post("/api/chat", json={"message": "在吗",
                                       "session_id": "ghost-session", "user_id": "u1"})
    assert r.status_code == 200
    assert "人工客服处理" in r.text


def test_normal_mode_unaffected(client):
    """没接管时行为逐字节不变。"""
    sid = _start_conversation(client)
    r = client.post("/api/chat", json={"message": "你好", "session_id": sid, "user_id": "u1"})
    assert r.status_code == 200
    assert "人工客服处理" not in r.text
