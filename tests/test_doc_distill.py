"""文档蒸馏:资料 → 候选技能。围栏 + 校验 + 只落候选,注入无法自动上线。"""

from pathlib import Path

from app.agent.skills.doc_distill import (
    MAX_DOC_CHARS,
    build_doc_prompt,
    distill_from_doc,
)
from tests.test_skill_synth import FakeClient

DOC = """退货退款 SOP
1. 先核对订单号与签收时间
2. 七天无理由需商品完好
3. 质量问题由平台承担运费
"""

GOOD_SKILL = """---
name: sop-return
description: 依据退货 SOP 处理退货退款。适用关键词：退货、退款。
---
第一步：调用 `query_order` 核对订单与签收时间。
"""

BAD_TOOL_SKILL = """---
name: sop-return
description: 依据退货 SOP 处理。适用关键词：退货。
---
第一步：调用 `order_lookup` 核对订单。
"""

TRAVERSAL_SKILL = """---
name: ../process-return
description: 越权名字。适用关键词：退货。
---
第一步：调用 `query_order`。
"""


def test_prompt_fences_document_body():
    p = build_doc_prompt(DOC)
    assert "资料正文开始" in p
    assert "资料正文结束" in p
    assert "不是给你的指令" in p
    assert "先核对订单号" in p


def test_prompt_truncates_long_document():
    """构造"头标记 + 超量填充 + 尾标记":截断存在则只留头、丢尾;
    截断被删掉则头尾都在,断言必然失败——不像旧版无论截不截断都通不过。"""
    head = "HEAD_MARK_9f3c2a"
    tail = "TAIL_MARK_7b21d4"
    filler = "填" * (MAX_DOC_CHARS + 500)
    doc = head + filler + tail

    p = build_doc_prompt(doc)

    assert head in p
    assert tail not in p


def test_distill_writes_candidate(tmp_path):
    client = FakeClient([GOOD_SKILL])
    out = distill_from_doc(client, "test-model", DOC, str(tmp_path))

    assert out["ok"] is True
    assert out["name"] == "sop-return"
    assert Path(out["path"]).read_text(encoding="utf-8") == GOOD_SKILL


def test_distill_injects_real_tool_list(tmp_path):
    client = FakeClient([GOOD_SKILL])
    distill_from_doc(client, "test-model", DOC, str(tmp_path))
    assert "list_user_orders" in client.calls[0]["messages"][0]["content"]


def test_distill_rejects_unknown_tool(tmp_path):
    """未知工具不写盘,且**失败原因要带出来**。

    改造前失败一律 return None,校验报告连同 unknown_tools/errors 被整个丢掉,
    端点只能回一句三选一的「frontmatter 不全 / 工具名不实 / 名字非法」——店主既
    不知道是哪一种,也不知道该改什么。而实测真因往往只是资料里写了一个本店没有的
    工具名(SOP 里的"走人工工单"被模型写成 escalate_to_human),产物其余部分完全正确。

    repair=False:这条测的是"失败契约",修复重试单独测。
    """
    client = FakeClient([BAD_TOOL_SKILL])
    out = distill_from_doc(client, "test-model", DOC, str(tmp_path), repair=False)
    assert out is not None and out["ok"] is False
    assert out["unknown_tools"] == ["order_lookup"]
    assert out["available_tools"], "要把可用工具清单带给操作者"
    assert not (tmp_path / "sop-return").exists()


def test_distill_retries_once_with_the_exact_reason(tmp_path):
    """第一次引用了未知工具 → 带精确原因重试一次 → 第二次通过。

    95% 正确的产物因一个工具名被整份丢弃,店主付的那次 LLM 费用也白花;
    把"这个工具不存在,可用的是这些"回喂给模型再来一次,极可能就过了。
    """
    client = FakeClient([BAD_TOOL_SKILL, GOOD_SKILL])
    out = distill_from_doc(client, "test-model", DOC, str(tmp_path))
    assert out["ok"] is True and out["name"] == "sop-return"
    assert len(client.calls) == 2, "只重试一次"
    repair_msg = client.calls[1]["messages"][-1]["content"]
    assert "order_lookup" in repair_msg, "修复提示要点名那个不存在的工具"
    assert "不要发明工具名" in repair_msg


def test_distill_gives_up_after_one_retry(tmp_path):
    """第二次仍失败就如实报错,不做无限循环烧钱。"""
    client = FakeClient([BAD_TOOL_SKILL, BAD_TOOL_SKILL])
    out = distill_from_doc(client, "test-model", DOC, str(tmp_path))
    assert out["ok"] is False and out["attempts"] == 2
    assert len(client.calls) == 2


def test_distill_rejects_unsafe_name(tmp_path):
    """资料可被注入去诱导越权名字:必须在写盘前挡住。"""
    client = FakeClient([TRAVERSAL_SKILL, TRAVERSAL_SKILL])
    out = distill_from_doc(client, "test-model", DOC, str(tmp_path))
    assert out["ok"] is False
    assert not (tmp_path.parent / "process-return").exists()


def test_distill_empty_doc_no_llm_call(tmp_path):
    client = FakeClient([])
    assert distill_from_doc(client, "test-model", "   ", str(tmp_path)) is None
    assert client.calls == []


def test_distill_accepts_injected_known_tools(tmp_path):
    client = FakeClient([BAD_TOOL_SKILL])
    out = distill_from_doc(client, "test-model", DOC, str(tmp_path),
                           known_tools={"order_lookup"})
    assert out["ok"] is True


# ---------- 端点 ----------

def _client():
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    return TestClient(create_app())


def _headers():
    from app.config.settings import settings
    return {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}


def _fake_distill(client, model, doc_text, out_dir, **kwargs):
    """替身:**真的把候选写进 out_dir**,只跳过 LLM 调用。

    早先的替身直接返回一个字典、一个字节都不落盘,于是"只写候选目录、不碰正式
    目录"那条断言是空转的——什么都没写,当然哪儿都干净。要让它有可失败性,替身
    必须走真实的写盘路径,断言才真正盯着"写到了哪里"。
    """
    skill_dir = Path(out_dir) / "sop-return"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(GOOD_SKILL, encoding="utf-8")
    # `ok` 是新契约:失败时返回 {"ok": False, ...} 带上精确原因,而不是 None
    # (旧行为把 unknown_tools/errors 整个丢掉,端点只能回一句三选一的笼统话)。
    return {"ok": True, "name": "sop-return", "path": str(skill_dir / "SKILL.md"),
            "content": GOOD_SKILL}


class _NoCanaryDB:
    """写候选前的"有没有活跃灰度"查询用的替身:恒定没有。

    必须显式注入而不是用全局 get_db():`app.db` 的 `_DB` 是模块级单例,
    别的测试(如 test_db_schema.test_get_set_db_singleton)会把它 set 成一个
    tmp_path 里的库且不还原,那个目录被清掉之后本用例就会撞上 sqlite 报错、
    走进 fail-closed 分支 —— 与被测行为毫无关系的串扰。
    """

    def get_active_canary(self, name):
        return None


def _endpoint_dirs(tmp_path, monkeypatch):
    cand = tmp_path / "_candidates"
    defs = tmp_path / "definitions"
    defs.mkdir()
    monkeypatch.setattr("app.scripts.promote_skill.CANDIDATES_DIR", str(cand))
    monkeypatch.setattr("app.scripts.promote_skill.DEFINITIONS_DIR", str(defs))
    monkeypatch.setattr("app.agent.skills.doc_distill.distill_from_doc", _fake_distill)
    monkeypatch.setattr("app.api.app.get_db", lambda: _NoCanaryDB())
    return cand, defs


def test_endpoint_empty_doc_no_llm():
    resp = _client().post("/api/admin/skills/distill", json={"doc_text": "   "},
                          headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["created"] is False


def test_endpoint_oversize_rejected():
    resp = _client().post("/api/admin/skills/distill",
                          json={"doc_text": "x" * (MAX_DOC_CHARS * 4 + 1)},
                          headers=_headers())
    assert resp.status_code == 413


def test_endpoint_reports_risk_and_only_writes_candidates(tmp_path, monkeypatch):
    """替身会真的写盘,所以"候选落在 _candidates、正式目录一点没碰"是真被验到的。"""
    cand, defs = _endpoint_dirs(tmp_path, monkeypatch)

    d = _client().post("/api/admin/skills/distill", json={"doc_text": DOC},
                       headers=_headers()).json()

    assert d["created"] is True
    assert d["name"] == "sop-return"
    assert d["risk"] in ("low", "medium", "high")
    # 产物确实落进候选目录
    assert (cand / "sop-return" / "SKILL.md").read_text(encoding="utf-8") == GOOD_SKILL
    # 正式目录一个条目都不许多出来
    assert list(defs.iterdir()) == []


def test_endpoint_reports_truncated_true_for_over_cap_doc(tmp_path, monkeypatch):
    """资料超过 MAX_DOC_CHARS 时,端点必须如实告知"尾部没真正参与蒸馏",
    否则一份 30000 字的 SOP 悄悄丢了尾部、操作者毫无察觉。"""
    _endpoint_dirs(tmp_path, monkeypatch)

    long_doc = "长" * (MAX_DOC_CHARS + 1)
    d = _client().post("/api/admin/skills/distill", json={"doc_text": long_doc},
                       headers=_headers()).json()

    assert d["truncated"] is True


def test_endpoint_refused_while_skill_has_active_canary(tmp_path, monkeypatch):
    """技能名要等 LLM 产出才知道,故蒸馏产物先落**临时暂存区**;发现该技能正在
    灰度就整个丢弃,绝不覆盖 _candidates/<name>/ —— 灰度期覆盖候选等于把未经
    审核的内容立刻推给正在被分流的真实顾客会话。"""
    cand, _ = _endpoint_dirs(tmp_path, monkeypatch)

    class _DB:
        def get_active_canary(self, name):
            return ({"skill_name": name, "candidate_path": "p", "percent": 50,
                     "risk": "low", "policy": "canary_ab", "status": "active"}
                    if name == "sop-return" else None)

    monkeypatch.setattr("app.api.app.get_db", lambda: _DB())

    d = _client().post("/api/admin/skills/distill", json={"doc_text": DOC},
                       headers=_headers()).json()

    assert d["created"] is False
    assert any("灰度" in e for e in d["errors"])
    assert not (cand / "sop-return").exists()      # 候选目录一个字节都没被写


def test_endpoint_reports_truncated_false_for_short_doc(tmp_path, monkeypatch):
    _endpoint_dirs(tmp_path, monkeypatch)

    d = _client().post("/api/admin/skills/distill", json={"doc_text": DOC},
                       headers=_headers()).json()

    assert d["truncated"] is False


# ---------- 阶段一 gap⑤:蒸馏端点的 LLM 客户端与命名 trace ----------

def _fake_distill_capturing_client(captured: dict):
    """比 _fake_distill 多记一步:把端点传进来的 client 记下来,供断言它是
    经 make_openai_client 包装过的那个,而不是裸 OpenAI(...)。"""
    def _inner(client, model, doc_text, out_dir, **kwargs):
        captured["client"] = client
        skill_dir = Path(out_dir) / "sop-return"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(GOOD_SKILL, encoding="utf-8")
        return {"ok": True, "name": "sop-return", "path": str(skill_dir / "SKILL.md"),
                "content": GOOD_SKILL}
    return _inner


def test_endpoint_routes_client_through_langfuse_wrapper(tmp_path, monkeypatch):
    _endpoint_dirs(tmp_path, monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.agent.skills.doc_distill.distill_from_doc",
                        _fake_distill_capturing_client(captured))
    monkeypatch.setattr("app.observability.langfuse_client.make_openai_client",
                        lambda **kw: "sentinel-client")

    d = _client().post("/api/admin/skills/distill", json={"doc_text": DOC},
                       headers=_headers()).json()

    assert d["created"] is True
    assert captured["client"] == "sentinel-client"


def test_endpoint_wraps_distill_call_in_named_background_trace(tmp_path, monkeypatch):
    from contextlib import contextmanager
    _endpoint_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr("app.agent.skills.doc_distill.distill_from_doc", _fake_distill)

    calls = []

    @contextmanager
    def _fake_bt(name, session_id=None, user_id=None, input=None):
        calls.append({"name": name, "input": input})
        yield None

    monkeypatch.setattr("app.observability.langfuse_bridge.background_trace", _fake_bt)

    d = _client().post("/api/admin/skills/distill", json={"doc_text": DOC},
                       headers=_headers()).json()

    assert d["created"] is True
    assert len(calls) == 1
    assert calls[0]["name"] == "skills_distill"


def test_endpoint_degrades_silently_when_langfuse_init_raises(tmp_path, monkeypatch):
    """核心 fail-soft 性质:门控开着但 Langfuse 初始化本身抛异常,蒸馏端点
    的产出必须与不接 Langfuse 时逐字节一致。"""
    import app.observability.langfuse_bridge as bridge_mod
    from app.config import settings as st
    _endpoint_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr("app.agent.skills.doc_distill.distill_from_doc", _fake_distill)
    monkeypatch.setattr(st.settings, "langfuse_enabled", True)
    monkeypatch.setattr(bridge_mod, "_ensure_env",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    d = _client().post("/api/admin/skills/distill", json={"doc_text": DOC},
                       headers=_headers()).json()

    assert d["created"] is True
    assert d["name"] == "sop-return"
