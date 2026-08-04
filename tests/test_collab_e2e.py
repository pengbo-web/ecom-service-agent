"""端到端:一次真实数据下的完整协作闭环(全程不打真实 LLM)。

场景 = 方案里的验收用例:某商品退款率跨线 → 参谋归因 → 营销起草 → 人工批准发出 →
按 correlation_id 全链路可回溯。

隔离说明:`get_db()` 默认是进程内单例、指向真实的 app/sessions/ecom.db,这张本地
开发用 scratch 库会跨多次运行累积数据。不隔离的话,本文件里对"至少几条""恰好
几条"的断言会被"上一次运行/别的会话留下的行"污染。做法与 tests/test_growth_api.py
一致:每个测试各自一份 tmp_path 下的临时 sqlite 文件,用 set_db() 换入换出,
测试结束后把单例复位,不影响同进程里其它测试文件,也不因运行历史而忽通忽不通。
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# 管理鉴权走 X-Admin-Token(见 app/hardening/auth.py::make_admin_auth),不是
# Authorization: Bearer —— 与 tests/test_seller_api.py、tests/test_growth_api.py
# 等既有 admin 端点测试用的头一致。
AUTH = {"X-Admin-Token": "T"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "collab_e2e_test.db"))
    db.init_schema()
    set_db(db)

    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


def _seed(db):
    """造出验收场景的真实数据:一款鞋 30% 退款率(跨 15% 告警线),外加一单
    "下单后久拖不发"的待处理订单,供营销侧 find_opportunities 命中并起草。

    订单状态用这张表真实存在的取值(pending / shipped / delivered /
    refund_processing);这个项目的订单表没有"未付款"这种状态,brief 草稿里
    的 status='unpaid' 在这张表上不成立,这里改用 refund_processing 表示
    "退款处理中"的订单,delivered 表示已完成的订单。
    """
    conn = db.connect()
    try:
        conn.execute("INSERT INTO products (product_id,name,category,price,stock) "
                     "VALUES ('P001','跑鞋','鞋类',199,50)")
        # 20 单里 6 单退款 → 30% ≥ 15% 告警线;都在扫描窗口(7 天)内。
        for i in range(20):
            refunded = i < 6
            conn.execute(
                "INSERT INTO orders (order_id,user,status,total,created_at,refund_status,"
                "refund_reason) VALUES (?,?,?,199,datetime('now'),?,?)",
                (f"ORD-{i}", f"u{i}",
                 "refund_processing" if refunded else "delivered",
                 "requested" if refunded else None,
                 "尺码不准" if refunded else None))
            conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                         "VALUES (?, '跑鞋','P001',1,199)", (f"ORD-{i}",))
        # 营销侧 find_opportunities(kind="stale_pending_order") 命中条件:
        # status='pending' 且下单时间早于 48 小时前、晚于窗口(14 天)起点——
        # 3 天前正好落在这个区间(与 tests/test_collab_pipeline.py::_unpaid 同口径)。
        conn.execute(
            "INSERT INTO orders (order_id,user,status,total,created_at) "
            "VALUES ('ORD-STALE-1','u-stale','pending',199,datetime('now','-3 days'))")
        conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                     "VALUES ('ORD-STALE-1','跑鞋','P001',1,199)")
        conn.commit()
    finally:
        conn.close()


def test_full_collaboration_closes_the_loop(client):
    from app.agent.tools.anomaly import scan_and_publish
    from app.db import get_db
    from app.multi_agent import bus, collab

    db = get_db()
    _seed(db)

    # ① 扫描 → 发信号(确定性,不碰 LLM)
    scanned = scan_and_publish(window_days=7)
    assert scanned["published"] >= 1
    corr = scanned["correlation_id"]

    # ②③ 参谋归因 → 营销起草(LLM 打桩,不花钱不看网络)。run_once() 一次调用
    # 就在同一次里先消费参谋段、再消费刚发布的营销段,足以把 signal→diagnosis→
    # drafts 整条链跑完(见 app/multi_agent/collab.py::run_once 文档字符串);
    # 这里只调一次,不是"分两阶段推进"。
    with patch.object(collab, "_llm_explain", return_value="尺码标注不符,建议按实测尺码表重标"), \
         patch.object(collab, "_llm_draft", return_value="这款鞋我们更新了尺码建议,可以参考下"):
        stats = collab.run_once()
    assert stats["analyst"]["done"] >= 1
    assert stats["growth"]["done"] >= 1

    drafts = db.list_outreach_drafts(status="draft")
    assert drafts, "营销侧应产出至少一条草稿"

    # ④ 人工批准 → 发出。投递函数挂在**这个 app 实例**的 app.state 上
    # (见 app/api/app.py create_app() 内的注释),不是模块级的 _deliver_outreach——
    # 打桩要打在这个实例上,与 tests/test_growth_api.py 的做法一致。
    with patch.object(client.app.state, "deliver_outreach", return_value=True):
        r = client.post(f"/api/admin/growth/drafts/{drafts[0]['id']}/approve", headers=AUTH)
    assert r.status_code == 200 and r.json()["sent"] is True

    # ⑤ 全链路按 correlation_id 可回溯:时间线端点应能重构这一条协作链的
    # 全部四个阶段——扫描信号、参谋诊断、营销草稿就绪、共享上下文里的诊断结论。
    tl = client.get(f"/api/admin/collab/timeline?correlation_id={corr}", headers=AUTH).json()
    kinds = {e["event_type"] for e in tl["events"]}
    assert bus.EV_SIGNAL_ANOMALY in kinds
    assert bus.EV_INSIGHT_DIAGNOSIS in kinds
    assert bus.EV_DRAFTS_READY in kinds
    assert any(s["key"].startswith("diagnosis:") for s in tl["shared"])


def test_no_llm_is_called_without_patching_in_scan(client):
    """扫描段必须是纯确定性的:不打桩也不会碰 LLM。"""
    import openai
    from app.agent.tools.anomaly import anomaly_scan
    from app.db import get_db

    _seed(get_db())
    with patch.object(openai, "OpenAI",
                      side_effect=AssertionError("扫描不得调用 LLM")):
        assert anomaly_scan(window_days=7)["anomalies"]


def test_buyer_chat_never_exposes_seller_tools(client):
    """买家画像不得拿到任何 B 端工具(经营数据泄漏红线)。"""
    from app.multi_agent.agents import AGENT_CONFIGS
    seller_only = {"shop_overview", "product_diagnostics", "service_quality",
                   "anomaly_scan", "find_opportunities", "draft_outreach",
                   "list_outreach_drafts"}
    for cfg in AGENT_CONFIGS.values():
        assert cfg["tools"] & seller_only == set()


def test_collab_disabled_leaves_buyer_path_untouched(client, monkeypatch):
    """开关关掉时,协作侧完全静默,买家链路零变化。"""
    from app.config import settings as st
    from app.db import get_db
    from app.multi_agent import collab

    monkeypatch.setattr(st.settings, "collab_enabled", False)
    before = len(get_db().list_events())
    stats = collab.run_once()
    assert stats["analyst"]["claimed"] == 0 and stats["growth"]["claimed"] == 0
    assert len(get_db().list_events()) == before


def test_timeline_requires_auth(client):
    assert client.get("/api/admin/collab/timeline").status_code in (401, 403)


def test_timeline_honours_console_switch(client, monkeypatch):
    """控制台开关关掉时,时间线端点也该 404,而不是继续正常返回。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "seller_console_enabled", False)
    r = client.get("/api/admin/collab/timeline", headers=AUTH)
    assert r.status_code == 404
