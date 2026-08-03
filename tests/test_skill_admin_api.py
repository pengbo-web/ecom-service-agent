"""管理端暴露自进化状态:现行技能 / 待审候选(含校验结论) / 各 skill 实战结局分布。"""

from fastapi.testclient import TestClient

from app.api.app import create_app


def _client():
    return TestClient(create_app())


def _headers():
    """admin_token 为空时后端不鉴权(见 app/hardening/auth.py),此时不必带头。"""
    from app.config.settings import settings
    return {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}


def test_admin_skills_returns_live_and_candidates():
    resp = _client().get("/api/admin/skills", headers=_headers())
    assert resp.status_code == 200

    data = resp.json()
    assert "live" in data and "candidates" in data and "traces" in data
    assert isinstance(data["live"], list)
    live_names = {s["name"] for s in data["live"]}
    assert "process-return" in live_names          # 正式库里的技能
    for skill in data["live"]:
        assert skill["description"]


def test_candidate_entries_carry_validation_verdict(monkeypatch):
    """用固定的两条候选夹具替换真实的 list_candidates,避免依赖仓库里
    未跟踪的 _candidates/ 目录(那属于环境态,可能为空,导致断言空转)。"""
    fixture = [
        {
            "name": "good-candidate",
            "path": "app/agent/skills/definitions/_candidates/good-candidate.yaml",
            "valid": True,
            "unknown_tools": [],
            "errors": [],
            "is_improvement": True,
        },
        {
            "name": "bad-candidate",
            "path": "app/agent/skills/definitions/_candidates/bad-candidate.yaml",
            "valid": False,
            "unknown_tools": ["nonexistent_tool"],
            "errors": ["unknown tool: nonexistent_tool"],
            "is_improvement": False,
        },
    ]
    monkeypatch.setattr("app.scripts.promote_skill.list_candidates", lambda *a, **k: fixture)

    data = _client().get("/api/admin/skills", headers=_headers()).json()

    assert len(data["candidates"]) == 2
    for item, expected in zip(data["candidates"], fixture):
        assert set(item) >= {"name", "valid", "unknown_tools", "errors", "is_improvement",
                              "risk", "policy"}
        assert isinstance(item["valid"], bool)
        for key, value in expected.items():
            assert item[key] == value
        # 夹具里的 path 并非磁盘上的真实文件,风险判定读不到内容——
        # 该候选仍必须出现(不能被这一项的异常拖累整份列表清空),
        # 只是 risk/policy 降级为 None,这正是"逐项容错"要覆盖的场景。
        assert item["risk"] is None
        assert item["policy"] is None


def test_traces_map_counts_outcomes(tmp_path, monkeypatch):
    from app.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    db.record_skill_trace("s1", "u1", "process-return", [], "success")
    db.record_skill_trace("s2", "u1", "process-return", [], "handoff")
    db.record_skill_trace("s3", "u1", "process-return", [], "handoff")
    # app/api/app.py 顶部用 `from app.db import get_db` 引入了函数引用,
    # 打 app.db.get_db 补丁不会影响该端点里已绑定的名字;需按本仓库既有
    # 惯例(见 test_api.py / test_users_api.py / test_auth_enforce.py)
    # 打 app.api.app.get_db 才能生效。
    monkeypatch.setattr("app.api.app.get_db", lambda: db)

    data = _client().get("/api/admin/skills", headers=_headers()).json()
    counts = data["traces"]["process-return"]
    assert counts["success"] == 1
    assert counts["handoff"] == 2
    assert counts.get("tool_error", 0) == 0


def test_traces_window_discloses_query_limit():
    """traces 段的计数来自有界、按时间倒序的窗口(全 skill 共用一个上限),
    高频 skill 会挤占低频 skill 的样本——响应必须把这个窗口值明示出来,
    且断言的是端点里真正传给查询的同一个常量,不能各说各话。"""
    from app.api.app import _TRACE_WINDOW

    data = _client().get("/api/admin/skills", headers=_headers()).json()

    assert data["traces_window"]["limit"] == _TRACE_WINDOW


# ---------- 分级授权可见性(Task 15) ----------

def test_candidates_carry_risk_and_policy():
    data = _client().get("/api/admin/skills", headers=_headers()).json()
    for item in data["candidates"]:
        assert item["risk"] in {"high", "medium", "low", None}
        assert item["policy"] in {"manual", "canary_ab", "gate_then_watch", None}


def test_active_canaries_exposed(tmp_path, monkeypatch):
    from app.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    db.start_canary("process-return", "/p/SKILL.md", 50, "low", "canary_ab")
    monkeypatch.setattr("app.api.app.get_db", lambda: db)

    data = _client().get("/api/admin/skills", headers=_headers()).json()

    assert len(data["canaries"]) == 1
    entry = data["canaries"][0]
    assert entry["skill_name"] == "process-return"
    assert entry["percent"] == 50
    assert entry["policy"] == "canary_ab"


# ---------- 风险档必须真的算对:is_new_skill 传反了会让新建 skill 看起来可自动上线 ----------

HIGH_RISK_CANDIDATE_MD = """---
name: process-return
description: 退货处理(引用动钱工具)。
---
第一步：调用 `query_order` 核对订单。
第二步：调用 `apply_refund` 提交退款。
"""

READONLY_CANDIDATE_MD = """---
name: order-query-all
description: 查全部订单。
---
第一步：调用 `list_user_orders`，需要明细再 `query_order`。
"""


def _fixture_candidate(tmp_path, name, content, is_improvement):
    """写一个真实候选文件,并返回 list_candidates 那种形状的条目。"""
    path = tmp_path / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"name": name, "path": str(path), "valid": True,
            "unknown_tools": [], "errors": [], "is_improvement": is_improvement}


def test_risk_and_policy_computed_from_real_content(tmp_path, monkeypatch):
    """三种档位必须各自算对,且能抓住 is_new_skill 传反:
    - 引用 apply_refund 的改进型 → high / manual(动钱,永远人工)
    - 纯只读的改进型            → low / canary_ab(可灰度自动上线)
    - 纯只读但**新建**          → medium / gate_then_watch(无对照组,先过门禁再守)
    若 is_new_skill 被传反,后两条会互换,本测试即失败。
    """
    from app.agent.skills.risk import (
        POLICY_CANARY_AB, POLICY_GATE_THEN_WATCH, POLICY_MANUAL,
        RISK_HIGH, RISK_LOW, RISK_MEDIUM,
    )

    fixture = [
        _fixture_candidate(tmp_path, "process-return", HIGH_RISK_CANDIDATE_MD, True),
        _fixture_candidate(tmp_path, "order-query-all", READONLY_CANDIDATE_MD, True),
        _fixture_candidate(tmp_path, "coupon-lookup", READONLY_CANDIDATE_MD, False),
    ]
    monkeypatch.setattr("app.scripts.promote_skill.list_candidates",
                        lambda cand_dir, def_dir: list(fixture))

    data = _client().get("/api/admin/skills", headers=_headers()).json()
    by_name = {c["name"]: c for c in data["candidates"]}

    assert len(by_name) == 3
    assert (by_name["process-return"]["risk"], by_name["process-return"]["policy"]) == (
        RISK_HIGH, POLICY_MANUAL)
    assert (by_name["order-query-all"]["risk"], by_name["order-query-all"]["policy"]) == (
        RISK_LOW, POLICY_CANARY_AB)
    assert (by_name["coupon-lookup"]["risk"], by_name["coupon-lookup"]["policy"]) == (
        RISK_MEDIUM, POLICY_GATE_THEN_WATCH)
