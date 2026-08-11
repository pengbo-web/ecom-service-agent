"""服务健康告警的判定窗口:与经营窗口分开,只看近况。

这组测试钉的是一件实测撞到的事:`track-order` 在 08-04 那天 36 次调用全部
tool_error(当时确实有缺陷),修好之后 08-11 当天 9 次全部成功。服务健康也走
7 天经营窗时,那 36 次仍留在分子里 → 报出 84% 失败率 → 一个**已经修好**的问题
连续报警 7 天、每轮扫描一条,协作链页面被 30 条同样的假警报刷满。

所以要同时钉两面,少一面都不算修好:
  ① 旧故障 + 近况健康 → 不报(这是修的目标);
  ② 近况就是坏的        → 照样报(否则等于把检测关了)。
"""

import datetime

import pytest

from app.agent.tools import anomaly


def _ago(days: float) -> str:
    """相对当前时间的时间戳字符串,与 skill_traces.created_at 同格式。"""
    t = datetime.datetime.now() - datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def scan(tmp_path, monkeypatch):
    """建一个只有 skill_traces 有内容的库,返回"塞行 → 扫描"的调用口。"""
    from app.db.database import Database

    db = Database(str(tmp_path / "ecom.db"))
    db.init_schema()

    from app.agent.tools import shop_analytics as sa
    from app.agent.tools import reviews as rv
    monkeypatch.setattr(sa, "get_db", lambda: db)

    # 商品/评价侧不是本组测试的对象,置空以免它们的异常混进断言。
    monkeypatch.setattr(sa, "product_diagnostics",
                        lambda **kw: {"products": []})
    monkeypatch.setattr(rv, "product_review_breakdown",
                        lambda *a, **kw: [])
    monkeypatch.setattr(anomaly, "_scanned_sku_total", lambda w: 0)
    monkeypatch.setattr(anomaly, "_scanned_review_total", lambda w: 0)

    def put(skill_name: str, outcome: str, days_ago: float, n: int = 1):
        conn = db.connect()
        try:
            for i in range(n):
                conn.execute(
                    "INSERT INTO skill_traces (session_id, skill_name, outcome, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (f"s-{skill_name}-{days_ago}-{i}", skill_name, outcome,
                     _ago(days_ago)))
            conn.commit()
        finally:
            conn.close()

    def run(**kw):
        return anomaly.anomaly_scan(**kw)

    return put, run


def _kinds(result, kind):
    return [a for a in result["anomalies"] if a["kind"] == kind]


def test_old_burst_with_healthy_recent_no_longer_alarms(scan):
    """实测场景本身:7 天前一整天全挂,今天全好 → 不该再报警。

    旧口径(服务健康也用 7 天窗)在这里会报出 ~78% 失败率。
    """
    put, run = scan
    put("track-order", "tool_error", days_ago=6.5, n=36)   # 旧故障
    put("track-order", "success", days_ago=0.1, n=9)        # 今天 9/9 健康

    r = run()
    assert _kinds(r, "tool_error_rate_high") == [], (
        "近 1 天 9 次全成功,不该因为 6 天前那批失败而报警")


def test_recent_breakage_still_alarms(scan):
    """另一面:近况就是坏的 → 必须照样报,否则这次改动等于把检测关掉了。"""
    put, run = scan
    put("track-order", "tool_error", days_ago=0.1, n=8)

    hits = _kinds(run(), "tool_error_rate_high")
    assert len(hits) == 1
    assert hits[0]["subject"] == "track-order"
    assert hits[0]["value"] == pytest.approx(1.0)


def test_finding_carries_its_own_window(scan):
    """告警必须自带口径:同样的 0.84,近 1 天和近 7 天是两件事,而下游(参谋的
    归因 prompt / 共享上下文 / 协作链页面)没有别的途径知道是哪一个。"""
    put, run = scan
    put("track-order", "tool_error", days_ago=0.1, n=8)

    hit = _kinds(run(), "tool_error_rate_high")[0]
    assert hit["detail"]["window_days"] == 1


def test_business_window_stays_wide(scan):
    """经营窗不能跟着缩:退款率/差评率有天然滞后,1 天窗会把它们压成 0。
    入参 window_days 仍按原样传给经营侧,并与服务窗一起报出来。"""
    put, run = scan
    r = run(window_days=7)
    assert r["window_days"] == 7
    assert r["service_window_days"] == 1


def test_insufficient_recent_samples_is_reported_not_swallowed(scan):
    """缩窗的代价要看得见:坏掉之后再没人调用的 skill,近窗样本不足,不再报警
    ——但必须出现在 service_insufficient 里,让"没报警"和"没数据所以报不了警"
    成为两件可分辨的事。"""
    put, run = scan
    put("track-order", "tool_error", days_ago=6.5, n=36)   # 旧故障,之后零调用

    r = run()
    assert _kinds(r, "tool_error_rate_high") == []
    assert r["service_insufficient"] == [
        {"skill_name": "track-order", "total": 0, "min_samples": 5}
    ], "近窗一条样本都没有,要如实报出来,不能静默跳过"


def test_service_window_is_configurable(scan, monkeypatch):
    """窗口是配置项,不是写死的字面量——低流量店铺可能需要放宽。"""
    put, run = scan
    put("track-order", "tool_error", days_ago=3.0, n=8)

    assert _kinds(run(), "tool_error_rate_high") == []   # 默认 1 天:够不着

    from app.config import settings as st
    monkeypatch.setattr(st.settings, "anomaly_service_window_days", 5)
    hits = _kinds(run(), "tool_error_rate_high")
    assert len(hits) == 1 and hits[0]["detail"]["window_days"] == 5


def test_thresholds_expose_the_service_window(scan):
    """口径要跟着 thresholds 一起出现:看到告警的人能据此判断这个比率的范围。"""
    put, run = scan
    assert run()["thresholds"]["service_window_days"] == 1
