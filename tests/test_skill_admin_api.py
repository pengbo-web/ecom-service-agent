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
        assert set(item) >= {"name", "valid", "unknown_tools", "errors", "is_improvement"}
        assert isinstance(item["valid"], bool)
        assert item == expected


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
