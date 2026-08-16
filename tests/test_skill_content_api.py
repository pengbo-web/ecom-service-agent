"""界面上能点开看一份技能到底写了什么。

**改造前这个页面上根本没有这个能力。** 它只显示 skill 的名字和 description ——
而 description 只是 frontmatter 里的一句话,真正决定客服说什么的是正文。
待审候选那一栏尤其要紧:操作者要在**没看过内容**的前提下点「转正上线」,
而那个按钮会立刻把这份正文推给线上会话。风险档、校验结论、门禁用例数全都齐了,
唯独缺了"它到底写了什么"。

本文件守三件事:
1. live 与 candidate 分开读(改进型候选与现行版同名,混起来会让人以为在看另一份);
2. skill 名不可信,必须挡住路径穿越(候选名一路来自 LLM 生成的 frontmatter,
   而它的素材是可被提示注入的顾客对话);
3. 正文**没读全**时必须显形——界面上少了一段内容,与"正文本来就这么长"看起来
   完全一样,而这里正是操作者据以决定要不要放行的地方。
"""

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


def _client():
    return TestClient(create_app())


def _headers():
    from app.config.settings import settings
    return {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}


def _get(name, variant=None):
    q = f"?variant={variant}" if variant else ""
    return _client().get(f"/api/admin/skills/{name}/content{q}", headers=_headers())


# --------------------------------------------------------------------------
# 正常读
# --------------------------------------------------------------------------

def test_reads_the_live_skill_body():
    r = _get("track-order")
    assert r.status_code == 200
    d = r.json()
    assert d["variant"] == "live"
    # 拿到的必须是**正文**,不是 description。frontmatter 在开头,说明是整份文件。
    assert d["content"].startswith("---")
    assert "name: track-order" in d["content"]
    assert len(d["content"]) > len("当用户想查订单状态"), "只拿到一句话,像是又只给了 description"


def test_reads_a_candidate_body():
    r = _get("order-query", variant="candidate")
    assert r.status_code == 200
    assert r.json()["variant"] == "candidate"
    assert "name: order-query" in r.json()["content"]


def test_live_and_candidate_are_separate_reads():
    """改进型候选与现行版**同名**。混起来会让人以为自己在看的是另一份——
    而这两份的差异正是他要决定放不放行的东西。"""
    live = _get("process-return").json()
    cand = _get("process-return", variant="candidate").json()
    assert live["path"] != cand["path"]
    assert "_candidates" in cand["path"] and "_candidates" not in live["path"]


def test_body_carries_version_and_fingerprint():
    """版本号与指纹是归因元数据:轨迹里记的就是这两个值,界面上看的那一份
    必须能和轨迹对上账,否则"这条失败来自哪一版"永远说不清。"""
    d = _get("track-order").json()
    assert d["version"] >= 1
    assert d["fingerprint"] and d["fingerprint"] != "unknown"


# --------------------------------------------------------------------------
# 名字不可信
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["_candidates", "_archive", ".version", "_swap"])
def test_underscore_helper_dirs_are_rejected(bad):
    """`_` 前缀的是辅助目录(候选/备份/中转),不是技能。
    `is_safe_skill_name` 拒掉它们,顺带也就拒掉了拿它当跳板的读法。"""
    assert _get(bad).status_code in (400, 404)


def test_path_traversal_in_the_name_is_rejected():
    """skill 名一路来自 LLM 生成的 frontmatter,素材是可被提示注入的顾客对话。"""
    r = _client().get("/api/admin/skills/..%2F..%2F.env/content", headers=_headers())
    assert r.status_code in (400, 404)
    assert "OPENAI_API_KEY" not in r.text


def test_bogus_variant_is_rejected_rather_than_silently_defaulting():
    """悄悄退回 live 会让人以为自己在看候选。"""
    r = _get("track-order", variant="wherever")
    assert r.status_code == 400


def test_missing_skill_is_404_and_says_which_side_was_searched():
    """"live 里没有"和"候选里没有"是两回事,报错要说清查的是哪一边。"""
    r = _get("no-such-skill")
    assert r.status_code == 404 and "live" in r.json()["detail"]


# --------------------------------------------------------------------------
# 没读全必须显形
# --------------------------------------------------------------------------

def test_disclosure_fields_are_always_present():
    """这四个字段是**披露**,不是错误路径的产物。前端要无条件读它们,
    所以后端必须无条件给,不能"没问题时就不带"。"""
    d = _get("track-order").json()
    for field in ("truncated", "over_cap", "unreadable", "escaped"):
        assert field in d, f"缺少披露字段 {field}"
    assert d["truncated"] is False and d["over_cap"] is False


def test_oversized_body_is_reported_as_truncated(tmp_path, monkeypatch):
    """一份超长正文只显示前一截,与"正文本来就这么长"在界面上完全一样。
    不报出来,操作者会在只看过开头的情况下点转正。"""
    from app.scripts import promote_skill as ps

    defs = tmp_path / "defs"
    (defs / "huge").mkdir(parents=True)
    (defs / "huge" / "SKILL.md").write_text(
        # 上限是 TREE_MAX_TOTAL_CHARS(40 万),要写超过它才会触顶
        "---\nname: huge\ndescription: d\n---\n" + "长" * 450000, encoding="utf-8")
    monkeypatch.setattr(ps, "DEFINITIONS_DIR", str(defs))

    d = _get("huge").json()
    assert d["over_cap"] is True, "总量触顶没有报出来"


def test_attachments_are_returned_because_they_go_live_too():
    """附带资料会随转正一起上线,并被 read_skill_file 灌进模型上下文。
    只审根 SKILL.md 等于放一条谁都不看的暗道(与 validate_skill_tree 同一条理由)。"""
    d = _get("track-order").json()
    assert "files" in d and "attachment_text" in d
