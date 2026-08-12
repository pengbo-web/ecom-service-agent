"""B1:多 Agent 编排器复用硬化引擎(EcomAgent)+ 按路由切画像。monkeypatch 避免联网。"""

from app.agent.chat import EcomAgent
from app.multi_agent.orchestrator import MultiAgentOrchestrator
from app.config.settings import settings


def _orch(tmp_path, monkeypatch):
    """建一个编排器,**并隔离数据库**。

    不隔离会读开发机上真实的 `app/sessions/ecom.db`,而那里可能存着一份自定义的
    店铺语气(实测:`shop_name='并夕夕旗舰店'`、正式语气、禁语「亲亲」,由某次
    `updated_by='cleanup'` 写入)。画像 prompt 在构造时经 `load_profile()` 把店铺
    语气渲染进去,于是 `test_chat_switches_profile_and_delegates` 的逐字比较会因为
    **开发机的数据库内容**而失败——排查时看到的是一段店名不同的 prompt diff,
    完全指不到真正的原因。

    `tests/test_growth_api.py` 早就为同一个问题写过隔离,注释也在那里:"get_db()
    默认是进程内单例、指向真实的 app/sessions/ecom.db…不隔离的话精确断言会被
    上一次运行/别的会话留下的行污染"。这里照同一份口径补上。
    """
    import app.db as db_mod
    from app.db import Database

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    path = str(tmp_path / "orchestrator_test.db")
    db = Database(db_path=path)
    db.init_schema()
    # 用 monkeypatch 直接接管那个单例,而不是 set_db()——**复位才有保证**:
    # set_db 改的是模块全局,测试结束后不会自己还原,会把污染带给同进程里后面跑的
    # 测试文件(而那种污染表现为"另一个文件里的测试莫名其妙地失败")。
    monkeypatch.setattr(db_mod, "_DB", db)
    monkeypatch.setattr(settings, "db_path", path)
    return MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")


def _tool_names(tm):
    return {d["function"]["name"] for d in tm.tool_definitions}


def test_engine_is_hardened_ecomagent(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    assert isinstance(o.engine, EcomAgent)          # 复用同一硬化引擎
    assert set(o.profiles) == {"presale", "midsale", "aftersale"}


def test_delegates_state_to_engine(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    assert o.raw_messages is o.engine.raw_messages
    assert o.memory_manager is o.engine.memory_manager
    assert o.session_id == o.engine.session_id
    assert o.user_id == "u1"


def test_profiles_expose_only_allowed_tools(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    presale = _tool_names(o.profiles["presale"]["tool_manager"])
    midsale = _tool_names(o.profiles["midsale"]["tool_manager"])
    aftersale = _tool_names(o.profiles["aftersale"]["tool_manager"])
    assert "apply_refund" in aftersale         # 售后能退款
    assert "apply_refund" not in presale       # 售前不能退款(工具隔离)
    assert "query_product" in presale          # 售前能查商品
    assert "change_address" in midsale         # 售中能改地址
    assert "change_address" not in presale     # 售前不能改地址
    assert "apply_refund" not in midsale       # 售中不做退款(交给售后)


def test_chat_switches_profile_and_delegates(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = lambda e: events.append(e)

    monkeypatch.setattr(o.router, "route", lambda *a, **k: "aftersale")
    captured = {}

    def fake_chat(user_input):
        captured["system_prompt"] = o.engine.system_prompt
        captured["tools"] = _tool_names(o.engine.tool_manager)
        captured["event_sink"] = o.engine.event_sink
        return "ok"

    monkeypatch.setattr(o.engine, "chat", fake_chat)
    out = o.chat("我要退款")

    assert out == "ok"
    # 切到了售后画像:prompt + 工具子集都换成 aftersale 的
    assert o.profiles["aftersale"]["prompt"] == captured["system_prompt"]
    assert "apply_refund" in captured["tools"]
    # event_sink 透传给了引擎;并发了 route 事件
    assert captured["event_sink"] is not None
    assert any(e.get("type") == "route" and e.get("key") == "aftersale" for e in events)
