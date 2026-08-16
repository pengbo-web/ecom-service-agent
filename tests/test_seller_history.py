"""参谋会话的历史回显 —— "离开页面再回来聊天记录没了"。

**后端一直在存,只是没有端点能读回来。** 参谋会话落在
`app/sessions/seller/<sid>.json`,内容完好;而卖家侧没有 history 端点,前端刷新
后无处可读,只能从空白重来。店主看到的是"记录丢了",实际是**存了但取不到**。
买家侧的 `/api/session/{id}/history` 早就有 —— 又一处"一条路做对了、孪生路没做"。

气泡重建复用同一个 `reconstruct_bubbles`,但要 `structured=False`:两侧落盘格式
不一样(见 app/api/history.py 的模块 docstring)。
"""

import json

import pytest

from app.api.history import reconstruct_bubbles


def _msgs(*pairs):
    out = []
    for role, content, tc in pairs:
        m = {"role": role, "content": content}
        if tc:
            m["tool_calls"] = [{"id": "x", "type": "function",
                                "function": {"name": "shop_overview", "arguments": "{}"}}]
        out.append(m)
    return out


# --------------------------------------------------------------------------
# 买家侧行为不能变
# --------------------------------------------------------------------------

def test_buyer_mode_is_unchanged():
    """买家侧最终回复是 `{"reply": ...}` 的 JSON,中间思考是纯文本。
    这条判据一个字都不能动 —— 它是既有页面的历史回显依据。"""
    msgs = _msgs(("user", "查订单", False),
                 ("assistant", "让我想想", False),          # 中间思考,应跳过
                 ("assistant", json.dumps({"reply": "已发货"}), False))
    assert reconstruct_bubbles(msgs) == [
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": "已发货"},
    ]


# --------------------------------------------------------------------------
# 卖家侧:最终回复是纯文本
# --------------------------------------------------------------------------

def test_plain_text_reply_is_kept():
    """**这就是记录"丢了"的原因。**

    参谋的最终回复落盘是纯文本,没有 `{"reply": ...}` 那层包装。拿买家口径去解析,
    每一条回复都会被当成"中间思考"跳过 —— 实测 4 条消息只重建出 1 个气泡,
    只剩用户那句。
    """
    msgs = _msgs(("user", "近7天GMV多少", False),
                 ("assistant", "", True),                    # 发起工具调用
                 ("tool", '{"gmv": 7823.0}', False),
                 ("assistant", "近7天GMV为7823.0元", False))

    assert len(reconstruct_bubbles(msgs)) == 1, "前提变了:买家口径不再丢弃纯文本回复"
    assert reconstruct_bubbles(msgs, structured=False) == [
        {"role": "user", "content": "近7天GMV多少"},
        {"role": "assistant", "content": "近7天GMV为7823.0元"},
    ]


def test_last_plain_text_wins_within_a_turn():
    """一轮里可能有多条纯文本 assistant(中途思考 + 收尾回复),要显示的是收尾那条。"""
    msgs = _msgs(("user", "问", False),
                 ("assistant", "我先看一下数据", False),
                 ("assistant", "", True),
                 ("tool", "{}", False),
                 ("assistant", "结论是 9 单", False))
    assert reconstruct_bubbles(msgs, structured=False)[-1]["content"] == "结论是 9 单"


def test_turn_order_is_user_then_assistant():
    """气泡顺序必须是 user → assistant → user → assistant,不能把上一轮的回复
    排到下一轮提问后面。"""
    msgs = _msgs(("user", "问一", False), ("assistant", "答一", False),
                 ("user", "问二", False), ("assistant", "答二", False))
    assert [b["content"] for b in reconstruct_bubbles(msgs, structured=False)] == \
        ["问一", "答一", "问二", "答二"]


def test_structured_replies_are_unwrapped_too():
    """**同一条参谋会话里两种格式并存。**

    多数回复是纯文本,但经 `_append_agent_reply` 之类路径写进去的仍是结构化 JSON。
    不拆的话页面上会直接渲染出
    `{"intent":"other","confidence":0.5,"reply":"样本量较小…"}` 整串给店主看
    —— 实测就是这样。
    """
    msgs = _msgs(("user", "出日报", False),
                 ("assistant", json.dumps({"intent": "other", "confidence": 0.5,
                                           "reply": "样本量较小,结论仅供参考"},
                                          ensure_ascii=False), False))
    assert reconstruct_bubbles(msgs, structured=False)[-1]["content"] == \
        "样本量较小,结论仅供参考"


@pytest.mark.parametrize("content", ["{不是合法 json", "{}", '{"no_reply": 1}'])
def test_json_looking_text_that_is_not_a_reply_is_shown_as_is(content):
    """拆包只在真的是 `{"reply": ...}` 时发生。以 `{` 开头但不是那个形状的内容,
    原样显示 —— 宁可显示得难看,也不要把参谋说的话吞掉。"""
    msgs = _msgs(("user", "问", False), ("assistant", content, False))
    assert reconstruct_bubbles(msgs, structured=False)[-1]["content"] == content


def test_empty_session_yields_no_bubbles():
    assert reconstruct_bubbles([], structured=False) == []


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------

def test_history_endpoint_returns_paired_bubbles(tmp_path, monkeypatch):
    """端到端:会话里有一问一答,读历史时两条都要在。

    用 tmp_path 起一个独立的 SessionManager,**不写真实的
    `app/sessions/seller/`**:`seller_sessions` 是模块级单例,直接往它里面塞会话
    会跨次累积(第一版就是这样,跑第二遍断言从 2 条变 6 条),而且污染的正是这个
    功能自己要读的那份数据。
    """
    from fastapi.testclient import TestClient

    from app.api import app as app_mod
    from app.api.session_manager import SessionManager
    from app.config.settings import settings

    isolated = SessionManager(agent_factory=app_mod._seller_factory,
                              base_dir=str(tmp_path / "seller"))
    monkeypatch.setattr(app_mod, "seller_sessions", isolated)

    sid = "hist-endpoint-probe"
    orch = isolated.get_or_create(sid, user_id="seller")
    orch.engine.raw_messages.extend([
        {"role": "user", "content": "近7天GMV多少"},
        {"role": "assistant", "content": "近7天GMV为7823.0元"},
    ])
    orch.save()

    client = TestClient(app_mod.create_app())
    headers = {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}
    r = client.get(f"/api/seller/{sid}/history", headers=headers)
    assert r.status_code == 200
    turns = r.json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["content"] == "近7天GMV为7823.0元"


def test_unknown_session_is_empty_not_an_error():
    """没聊过的会话回空列表 —— 前端据此显示空态,而不是弹一个错误。"""
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.config.settings import settings

    client = TestClient(create_app())
    headers = {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}
    r = client.get("/api/seller/never-chatted-here/history", headers=headers)
    assert r.status_code == 200 and r.json()["turns"] == []
