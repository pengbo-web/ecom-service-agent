"""Skill 版本身份:版本号写在技能目录里,随目录搬运天然一致。

为什么不放数据库:技能的身份跟着**目录**走——上传、转正、回滚都是整目录搬运
(见 promote_skill._replace_tree),版本号放目录里就自动跟着走;放数据库要额外
维护"目录状态与表状态同步",而这条链已经被 TOCTOU 咬过一次(见转正快照的注释)。

读不到/读坏一律回落 1:版本号是用来**归因**的,坏数据不该让加载失败。

## 整数版本号解决不了的问题(finding 1)

`.version` 只在 `promote()`/`rollback()` 写正式(live)目录时才会被打上,**候选
目录(`_candidates/<name>/`)从来不会有这个文件**——不是漏写,而是候选的产生
路径有三处(人工上传、`synthesize_skills` 合成、文档蒸馏),没有一处需要关心
"这是第几版",也不该为了这一个字段去改三处调用方。后果是:灰度期 `load_skill`
从候选目录读版本号,读到的永远是"文件不存在 → 1"——同一个 skill 前后两批不同
内容的候选,轨迹里的 `skill_version` 全部是 1,彻底无法区分。

修法:再加一个**内容指纹**(`fingerprint_skill_dir`),与整数版本号并存而不是
取代它——整数版本号仍然照旧只服务"这个 skill 名字转正过几次"这条单调计数
(promote/rollback 语义不变);指纹则服务"当前这棵树的字节内容是谁"这条正交
的问题,不需要在候选创建的任何一处打标,直接在 `load_skill` 实际被服务的那一刻
现算,天然覆盖 live 与 candidate 两种情况:
  - 两份内容不同的候选 → sha256 不同,不会被误判成同一版本;
  - 同一份内容不管是被 live 服务还是被 canary 服务 → 字节相同,指纹相同,
    二者的轨迹因此可比较(这条正是"live 与 canary 若内容相同应可比"的约束)。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

VERSION_FILE = ".version"
UNKNOWN_FINGERPRINT = "unknown"


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


def fingerprint_skill_dir(skill_dir: Path) -> str:
    """对该技能目录**当前被服务的那棵树**做确定性内容指纹(sha256,取前 16 位十六进制)。

    逐文件(按相对路径排序,确定性遍历顺序)把"相对路径 + 内容字节"喂进同一个
    哈希——两份目录只要有任何一个文件的路径或字节不同,指纹必然不同(sha256 抗
    碰撞:不会有"不同内容撞同一个指纹"这回事,只有"同内容永远同指纹"和"读不出
    时退化成 unknown"两种结果)。

    刻意跳过顶层 `.version` 本身:否则同一份技能正文,被 live 服务(目录里有
    `.version`)和被 candidate 服务(候选目录里从不带 `.version`)会算出两个不同
    的指纹——那就违反了"内容相同应可比较"这条约束,而且会让 promote/rollback
    每次改写 `.version` 都连带把指纹变掉,对"归因"这个目的毫无意义。

    读不到目录/任何一个文件读取失败一律返回 "unknown"(归因用的软数据,不能
    因为某个文件一时半会儿读不出——权限、并发写入中——就让 load_skill 本身失败)。
    """
    try:
        d = Path(skill_dir)
        if not d.is_dir():
            return UNKNOWN_FINGERPRINT
        h = hashlib.sha256()
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(d).as_posix()
            if rel == VERSION_FILE:
                continue
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
        return h.hexdigest()[:16]
    except OSError:
        return UNKNOWN_FINGERPRINT
