"""技能包(ZIP)安全解压。

Agent Skills 标准里一个技能是**目录**(SKILL.md + 可选参考资料),故上传形态是压缩包。
但 ZIP 的信任面比单文件大得多:单文件只有一个 name 要校验,ZIP 里**每个条目路径都是
上传者可控的**。本模块负责把这些都挡住:

  - zip slip:条目含 `..` 或绝对路径 → 解压时逃出目标目录;
  - zip bomb:压缩比攻击 → **不信 zip 头声明的大小**,按写盘实际字节累计并设上限;
  - 符号链接条目:解压出的符链可指向目标目录外;
  - 三重上限:条目数 / 单文件大小 / 解压后总大小。

本模块**不执行**任何解压出来的内容,只把它们当文本资料落盘。
"""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import PurePosixPath

MAX_ENTRIES = 200              # 条目数上限
MAX_FILE_BYTES = 2_000_000     # 单个文件解压后大小上限
MAX_TOTAL_BYTES = 10_000_000   # 解压后总大小上限
_CHUNK = 65536

# 压缩工具常带进来的元数据,直接忽略(不算进条目数,也不落盘)
_IGNORED_PREFIXES = ("__MACOSX/", "__MACOS/")
_IGNORED_NAMES = (".DS_Store", "Thumbs.db")


class BundleError(ValueError):
    """技能包不合规(结构、路径或体积)。"""


def _is_ignored(name: str) -> bool:
    if name.startswith(_IGNORED_PREFIXES):
        return True
    tail = PurePosixPath(name).name
    return tail in _IGNORED_NAMES or tail.startswith("._")


def _safe_rel(name: str) -> PurePosixPath:
    """把条目名归一成安全相对路径;不合规抛 BundleError。"""
    raw = name.replace("\\", "/")
    p = PurePosixPath(raw)
    if p.is_absolute() or raw.startswith("/"):
        raise BundleError(f"拒绝绝对路径条目: {name}")
    if any(part == ".." for part in p.parts):
        raise BundleError(f"拒绝越出目录的条目: {name}")
    if len(raw) > 1 and raw[1] == ":":          # Windows 盘符 C:/...
        raise BundleError(f"拒绝带盘符的条目: {name}")
    return p


def _strip_single_top_dir(rels: list[PurePosixPath]) -> str:
    """包里若统一套了一层目录(如 demo-skill/…),返回该目录名以便剥掉;否则返回 ""。"""
    tops = {r.parts[0] for r in rels if len(r.parts) > 1}
    flat = [r for r in rels if len(r.parts) == 1]
    if len(tops) == 1 and not flat:
        return next(iter(tops))
    return ""


def extract_skill_bundle(data: bytes, dest_dir: str) -> dict:
    """把技能包解压到 dest_dir,返回 {"skill_md": 绝对路径, "files": 相对路径列表}。

    dest_dir 会被创建(已存在则复用)。任何不合规一律抛 BundleError,且**不留下**
    目标目录之外的任何文件。
    """
    from pathlib import Path

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleError(f"不是有效的压缩包: {exc}") from exc

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and not _is_ignored(i.filename)]
        if len(infos) > MAX_ENTRIES:
            raise BundleError(f"条目过多({len(infos)} > {MAX_ENTRIES})")

        for info in infos:
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise BundleError(f"拒绝符号链接条目: {info.filename}")

        rels = [_safe_rel(i.filename) for i in infos]
        top = _strip_single_top_dir(rels)
        normalized = [
            PurePosixPath(*r.parts[1:]) if top and r.parts[0] == top else r
            for r in rels
        ]
        if not any(r.as_posix() == "SKILL.md" for r in normalized):
            raise BundleError("压缩包里没有 SKILL.md(技能包必须在根目录或单层目录下含 SKILL.md)")

        root = Path(dest_dir)
        root.mkdir(parents=True, exist_ok=True)
        total = 0
        written: list[str] = []

        for info, rel in zip(infos, normalized):
            if not rel.parts:
                continue
            target = root / Path(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            try:
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        # 不信 zip 头声明的大小,按实际写入字节判上限
                        if size > MAX_FILE_BYTES:
                            raise BundleError(f"单个文件过大: {rel.as_posix()}")
                        if total > MAX_TOTAL_BYTES:
                            raise BundleError(f"解压后总大小超限(> {MAX_TOTAL_BYTES} 字节)")
                        dst.write(chunk)
            except (zipfile.BadZipFile, OSError) as exc:
                # 条目声明的大小/CRC 与实际解压流不符(头部被篡改或包已损坏),
                # 同样不能当作"合规"处理,统一归一成 BundleError。
                raise BundleError(f"条目已损坏或与声明不符: {rel.as_posix()}: {exc}") from exc
            written.append(rel.as_posix())

        return {"skill_md": str(root / "SKILL.md"),
                "files": sorted(f for f in written if f != "SKILL.md")}
