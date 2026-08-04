"""上传技能包:必须落 _candidates 并过与 LLM 产物同一套关卡,绝不写正式目录。"""

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app

GOOD_MD = """---
name: upload-demo
description: 上传来的演示技能。适用关键词：演示、上传。
---
第一步：调用 `query_order` 核对订单。详见 references/policy.md。
"""

UNKNOWN_TOOL_MD = """---
name: upload-bad-tool
description: 引用了不存在的工具。适用关键词：演示。
---
第一步：调用 `order_list` 拉订单。
"""

TRAVERSAL_NAME_MD = """---
name: ../process-return
description: 越权名字。适用关键词：演示。
---
第一步：调用 `query_order`。
"""


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


def _headers():
    from app.config.settings import settings
    return {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}


def _upload(filename: str, data: bytes):
    return TestClient(create_app()).post(
        "/api/admin/skills/upload",
        files={"file": (filename, data, "application/octet-stream")},
        headers=_headers())


def _dirs(tmp_path, monkeypatch):
    cand = tmp_path / "_candidates"
    defs = tmp_path / "definitions"
    defs.mkdir()
    monkeypatch.setattr("app.scripts.promote_skill.CANDIDATES_DIR", str(cand))
    monkeypatch.setattr("app.scripts.promote_skill.DEFINITIONS_DIR", str(defs))
    return cand, defs


def test_zip_bundle_lands_in_candidates(tmp_path, monkeypatch):
    cand, defs = _dirs(tmp_path, monkeypatch)
    data = _zip({"upload-demo/SKILL.md": GOOD_MD,
                 "upload-demo/references/policy.md": "政策正文"})

    resp = _upload("skill.zip", data)

    assert resp.status_code == 200
    d = resp.json()
    assert d["accepted"] is True
    assert d["name"] == "upload-demo"
    assert d["files"] == ["references/policy.md"]
    assert (cand / "upload-demo" / "SKILL.md").read_text(encoding="utf-8") == GOOD_MD
    assert (cand / "upload-demo" / "references" / "policy.md").exists()
    assert list(defs.iterdir()) == []          # 正式目录未被写入


def test_single_md_upload_still_supported(tmp_path, monkeypatch):
    cand, _ = _dirs(tmp_path, monkeypatch)
    d = _upload("SKILL.md", GOOD_MD.encode("utf-8")).json()

    assert d["accepted"] is True
    assert d["files"] == []
    assert (cand / "upload-demo" / "SKILL.md").exists()


def test_second_upload_reports_replaced(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch)
    _upload("SKILL.md", GOOD_MD.encode("utf-8"))
    d = _upload("SKILL.md", GOOD_MD.encode("utf-8")).json()
    assert d["replaced"] is True


def test_unknown_tool_rejected_and_nothing_written(tmp_path, monkeypatch):
    cand, _ = _dirs(tmp_path, monkeypatch)
    d = _upload("SKILL.md", UNKNOWN_TOOL_MD.encode("utf-8")).json()

    assert d["accepted"] is False
    assert "order_list" in d["unknown_tools"]
    assert not (cand / "upload-bad-tool").exists()


def test_traversal_name_rejected(tmp_path, monkeypatch):
    """上传是外部可控入口:frontmatter 里的 ../ 名字必须挡在写盘前。"""
    cand, defs = _dirs(tmp_path, monkeypatch)
    (defs / "process-return").mkdir()
    (defs / "process-return" / "SKILL.md").write_text("线上原版", encoding="utf-8")

    d = _upload("SKILL.md", TRAVERSAL_NAME_MD.encode("utf-8")).json()

    assert d["accepted"] is False
    assert (defs / "process-return" / "SKILL.md").read_text(encoding="utf-8") == "线上原版"


def test_zip_slip_entry_rejected(tmp_path, monkeypatch):
    cand, _ = _dirs(tmp_path, monkeypatch)
    data = _zip({"SKILL.md": GOOD_MD, "../evil.md": "坏"})

    d = _upload("skill.zip", data).json()

    assert d["accepted"] is False
    assert not (tmp_path / "evil.md").exists()


def test_zip_without_skill_md_rejected(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch)
    d = _upload("skill.zip", _zip({"references/p.md": "x"})).json()
    assert d["accepted"] is False
    assert any("SKILL.md" in e for e in d["errors"])


def test_oversize_upload_rejected(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch)
    from app.api.app import _MAX_UPLOAD_BYTES
    resp = _upload("skill.zip", b"x" * (_MAX_UPLOAD_BYTES + 1))
    assert resp.status_code == 413
