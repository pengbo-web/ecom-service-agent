"""ZIP 安全解压:zip slip / zip bomb / 符号链接 / 三重上限,全部必须挡住。"""

import io
import zipfile

import pytest

from app.agent.skills.bundle import (
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    BundleError,
    extract_skill_bundle,
)

SKILL_MD = """---
name: demo-skill
description: 演示。适用关键词:演示。
---
正文。
"""


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


# ---------- 正常包 ----------

def test_extracts_flat_bundle(tmp_path):
    data = _zip({"SKILL.md": SKILL_MD, "references/policy.md": "政策"})
    out = extract_skill_bundle(data, str(tmp_path))

    assert out["files"] == ["references/policy.md"]
    from pathlib import Path
    assert Path(out["skill_md"]).read_text(encoding="utf-8") == SKILL_MD
    assert (tmp_path / "references" / "policy.md").read_text(encoding="utf-8") == "政策"


def test_extracts_bundle_with_single_top_dir(tmp_path):
    """常见形态:压缩包里套一层目录,应被剥掉。"""
    data = _zip({"demo-skill/SKILL.md": SKILL_MD, "demo-skill/references/p.md": "政策"})
    out = extract_skill_bundle(data, str(tmp_path))

    assert out["files"] == ["references/p.md"]
    assert (tmp_path / "SKILL.md").exists()


def test_ignores_macos_metadata(tmp_path):
    data = _zip({"SKILL.md": SKILL_MD, "__MACOSX/._SKILL.md": "junk",
                 ".DS_Store": "junk"})
    out = extract_skill_bundle(data, str(tmp_path))
    assert out["files"] == []


# ---------- 必须拒绝 ----------

def test_rejects_missing_skill_md(tmp_path):
    with pytest.raises(BundleError, match="SKILL.md"):
        extract_skill_bundle(_zip({"references/p.md": "x"}), str(tmp_path))


def test_rejects_path_traversal_entry(tmp_path):
    """zip slip:条目路径逃出目标目录必须拒,且不得在目标外留下文件。"""
    data = _zip({"SKILL.md": SKILL_MD, "../evil.md": "坏"})
    with pytest.raises(BundleError):
        extract_skill_bundle(data, str(tmp_path / "dest"))
    assert not (tmp_path / "evil.md").exists()


def test_rejects_absolute_entry(tmp_path):
    data = _zip({"SKILL.md": SKILL_MD, "/etc/passwd": "坏"})
    with pytest.raises(BundleError):
        extract_skill_bundle(data, str(tmp_path))


def test_rejects_too_many_entries(tmp_path):
    entries = {"SKILL.md": SKILL_MD}
    entries.update({f"f{i}.md": "x" for i in range(MAX_ENTRIES + 1)})
    with pytest.raises(BundleError, match="条目"):
        extract_skill_bundle(_zip(entries), str(tmp_path))


def test_rejects_oversize_single_file(tmp_path):
    data = _zip({"SKILL.md": SKILL_MD, "big.md": "x" * (MAX_FILE_BYTES + 1)})
    with pytest.raises(BundleError, match="单个文件"):
        extract_skill_bundle(data, str(tmp_path))


def test_rejects_zip_bomb_by_actual_bytes(tmp_path):
    """压缩比攻击:头里声明的大小不可信,必须按写盘的实际字节累计。"""
    # 高度可压缩内容:压缩后很小,解压后远超总量上限
    per = MAX_FILE_BYTES - 1
    count = (MAX_TOTAL_BYTES // per) + 2
    entries = {"SKILL.md": SKILL_MD}
    entries.update({f"b{i}.md": "0" * per for i in range(count)})
    with pytest.raises(BundleError, match="总大小"):
        extract_skill_bundle(_zip(entries), str(tmp_path))


def test_rejects_symlink_entry(tmp_path):
    """符号链接条目可指向目标目录外,必须拒。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", SKILL_MD)
        info = zipfile.ZipInfo("link.md")
        info.external_attr = (0xA1FF << 16)      # S_IFLNK | 0777
        z.writestr(info, "/etc/passwd")
    with pytest.raises(BundleError, match="符号链接"):
        extract_skill_bundle(buf.getvalue(), str(tmp_path))


def test_rejects_not_a_zip(tmp_path):
    with pytest.raises(BundleError, match="压缩包"):
        extract_skill_bundle(b"this is not a zip", str(tmp_path))


def test_corrupted_entry_raises_bundle_error(tmp_path):
    """条目数据被篡改(CRC 对不上)时必须抛 BundleError,不能漏出 zipfile 的原始异常。

    用 ZIP_STORED 写入,明文原样落在压缩包字节里,翻转其中一个字节即可造成
    CRC 不符 —— 这是可确定复现的损坏条目。
    """
    payload = b"REFERENCE-PAYLOAD-0123456789"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("SKILL.md", SKILL_MD)
        z.writestr("references/p.md", payload.decode())

    raw = bytearray(buf.getvalue())
    idx = raw.find(payload)
    assert idx > 0, "ZIP_STORED 下明文应原样存储,未找到待篡改位置"
    raw[idx] ^= 0xFF                       # 数据变了但 CRC 没变 → 读取时校验失败

    with pytest.raises(BundleError):
        extract_skill_bundle(bytes(raw), str(tmp_path))
