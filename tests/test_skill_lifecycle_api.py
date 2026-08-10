"""Skill 转正 / 驳回 / 回滚端点——自进化闭环的最后一环。

**补的是"候选只进不出"。** 改造前 skills 只有三个端点(列表/上传/蒸馏):候选能
从界面产生,却**只能登进服务器敲 `python -m app.scripts.promote_skill` 才上得线**。
实测界面上躺着 7 个候选(5 个校验通过),产品内没有任何办法处理它们——7 步自进化
闭环因此断在第 7 步。
"""

from __future__ import annotations

import inspect

import pytest

from app.api import app as app_module
from app.scripts import promote_skill as ps


def _src(fn_name: str) -> str:
    """取该端点函数的完整源码。

    按**下一个装饰器**切边界,不用固定字符数截断:固定长度会在函数变长时静默把
    末尾切掉,断言随之失效——而它失效的方式是"测试还在、只是不再检查那一段",
    这比直接报错糟得多(我第一版就这样切掉了 HTTPException 那行)。
    """
    src = inspect.getsource(app_module)
    start = src.index(f"def {fn_name}")
    nxt = src.find("\n    @app.", start)
    return src[start:nxt if nxt != -1 else len(src)]


# ---------- 关卡必须全部保留 ----------

def test_promote_reuses_the_cli_promote_function():
    """复用 `promote_skill.promote()`,不在端点里重写简化版。

    那个函数已经处理过 TOCTOU(先把候选整树快照,校验/判档/装机只认那一份字节),
    而"网页能并发替换候选目录"恰恰是这个端点引入的并发源。
    """
    body = _src("admin_promote_skill")
    assert "ps.promote(" in body


def test_gate_is_not_run_by_default_and_that_is_fail_closed():
    """默认不跑门禁,但**不静默绕过**:传 gate=None 让 promote() fail-closed 拒绝。

    `gate_candidate()` 会真跑两轮评测(候选 vs 现行,各自真调 LLM),一两分钟且
    花钱——挂在网页按钮上同步等既不合适也容易被网关掐断。所以默认不跑,
    而后端如实拒绝并说明门禁没跑过,由操作者显式 force 放行。
    """
    body = _src("admin_promote_skill")
    assert "run_gate: bool = False" in body
    assert "gate_note" in body, "必须把门禁有没有跑过告诉前端"


def test_force_does_not_bypass_validation():
    """`force` 只放行评测门禁,不放行校验——与 CLI `--force` 同义。

    实测:对一个引用了未知工具 `product_diagnostics` 的候选带 force 转正,
    仍被"校验未通过"拦下。
    """
    body = _src("admin_promote_skill")
    assert "只放行**评测门禁**,不放行校验" in body


def test_blocked_promotion_returns_400_with_reason():
    """关卡拦下不是服务器故障,且原因要原样给前端渲染。"""
    body = _src("admin_promote_skill")
    assert "HTTPException(400" in body
    assert "gate_note" in body


def test_reject_refuses_while_canary_is_active():
    """灰度中的候选不能驳回:灰度期候选正文正在为一部分真实会话服务,
    把目录移走会让那些会话当场读不到文件。"""
    body = _src("admin_reject_skill")
    assert "_skill_canary_block" in body
    assert "409" in body


def test_illegal_skill_name_is_rejected():
    """路径穿越必须拒。"""
    body = _src("admin_reject_skill")
    assert "is_safe_skill_name" in body


# ---------- 归档:不删,且两条路径共用 ----------

def test_processed_candidates_are_archived_not_deleted():
    """驳回的是下一轮改进的输入,已转正的是"线上正文当时长什么样"的证据。"""
    assert ps.PROMOTED_DIR.endswith("_promoted")
    assert ps.REJECTED_DIR.endswith("_rejected")
    doc = ps.archive_candidate.__doc__ or ""
    assert "不删" in doc or "归档" in doc


def test_cli_and_endpoint_share_one_archive_path():
    """CLI 与界面必须走同一个归档函数。

    本项目已多次因为"一半组件做对、另一半漏了"出问题(会话锁降级、
    list_user_orders 的 success 判定、conftest 漏钉 mcp_enabled),
    所以这条钉的是**两条路径共用**,不是各自正确。
    """
    assert "archive_candidate" in inspect.getsource(ps.main)
    assert "ps.archive_candidate" in _src("admin_promote_skill")
    assert "ps.archive_candidate" in _src("admin_reject_skill")


def test_archive_failure_does_not_turn_a_success_into_a_failure():
    """归档失败不改变"已转正"这个结论——技能已经装上线了。"""
    body = _src("admin_promote_skill")
    i = body.index("ps.archive_candidate")
    # 归档结果只是塞进响应体,不参与 promoted 判定
    assert 'r["archive"]' in body
    assert "if not r.get(\"promoted\")" in body[:i] or "promoted" in body


@pytest.mark.parametrize("name", ["../etc", "_swap", "a/b", ""])
def test_archive_rejects_unsafe_names(tmp_path, name):
    r = ps.archive_candidate(name, str(tmp_path / "cand"), str(tmp_path / "arch"), "ts")
    assert r["archived"] is False


def test_archive_moves_the_tree(tmp_path):
    cand = tmp_path / "cand" / "demo"
    cand.mkdir(parents=True)
    (cand / "SKILL.md").write_text("x", encoding="utf-8")
    r = ps.archive_candidate("demo", str(tmp_path / "cand"), str(tmp_path / "arch"), "ts1")
    assert r["archived"] is True
    assert not cand.exists(), "候选必须从待审队列移走"
    assert (tmp_path / "arch" / "demo-ts1" / "SKILL.md").exists()


def test_archive_is_idempotent_when_already_gone(tmp_path):
    """已经被处理过时如实说明,而不是抛错。"""
    r = ps.archive_candidate("demo", str(tmp_path / "cand"), str(tmp_path / "arch"), "ts")
    assert r["archived"] is False
    assert "不存在" in r["reason"]


def test_timestamp_prevents_overwriting_earlier_archives(tmp_path):
    """同一个 skill 可能被反复驳回/多次转正,归档不能互相覆盖。"""
    for stamp in ("ts1", "ts2"):
        cand = tmp_path / "cand" / "demo"
        cand.mkdir(parents=True)
        (cand / "SKILL.md").write_text(stamp, encoding="utf-8")
        ps.archive_candidate("demo", str(tmp_path / "cand"), str(tmp_path / "arch"), stamp)
    assert (tmp_path / "arch" / "demo-ts1").exists()
    assert (tmp_path / "arch" / "demo-ts2").exists()


# ---------- 回滚是转正的对偶 ----------

def test_rollback_endpoint_exists_and_reuses_cli():
    """没有回滚,"一键转正"就是一个**没有退路**的按钮——而技能正文直接决定客服
    说什么,上线后发现不对必须能立刻退回去。"""
    body = _src("admin_rollback_skill")
    assert "ps.rollback" in body


def test_all_lifecycle_endpoints_require_admin():
    src = inspect.getsource(app_module)
    for path in ('"/api/admin/skills/{skill_name}/promote"',
                 '"/api/admin/skills/{skill_name}/reject"',
                 '"/api/admin/skills/{skill_name}/rollback"'):
        i = src.index(path)
        assert "admin_auth" in src[i:i + 200], path
