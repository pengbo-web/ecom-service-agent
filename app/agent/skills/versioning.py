"""Skill 版本身份:版本号写在技能目录里,随目录搬运天然一致。

为什么不放数据库:技能的身份跟着**目录**走——上传、转正、回滚都是整目录搬运
(见 promote_skill._replace_tree),版本号放目录里就自动跟着走;放数据库要额外
维护"目录状态与表状态同步",而这条链已经被 TOCTOU 咬过一次(见转正快照的注释)。

读不到/读坏一律回落 1:版本号是用来**归因**的,坏数据不该让加载失败。
"""

from __future__ import annotations

from pathlib import Path

VERSION_FILE = ".version"


def read_version(skill_dir: Path) -> int:
    """读该技能目录的版本号。无文件/坏内容/非正整数 → 1。"""
    try:
        raw = (Path(skill_dir) / VERSION_FILE).read_text(encoding="utf-8").strip()
        v = int(raw)
        return v if v >= 1 else 1
    except (OSError, ValueError, TypeError):
        return 1


def bump_version(skill_dir: Path) -> int:
    """版本号 +1 并写回,返回新版本号。目录不存在时抛 OSError(调用方该知道)。"""
    d = Path(skill_dir)
    new = read_version(d) + 1
    (d / VERSION_FILE).write_text(str(new), encoding="utf-8")
    return new
