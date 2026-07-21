"""超大工具结果落盘存储（借鉴 nanobot utils/helpers.py::maybe_persist_tool_result）。

超阈值的工具结果写盘,历史里只留一个"可再读取"的 ref 指针 + 预览,信息零丢失;
模型需要完整内容时用 read_tool_result(ref, offset, length) 分段读回。
"""

import os
import re
import uuid
from pathlib import Path
from typing import Optional

from app.config.settings import settings

_REF_RE = re.compile(r"^tr_[a-z0-9]+$")   # 限定 ref 形态,防路径穿越


class ToolResultStore:
    def __init__(self, base_dir: Optional[str] = None, id_factory=None):
        self.base_dir = Path(base_dir or settings.tool_result_dir)
        self._id = id_factory or (lambda: "tr_" + uuid.uuid4().hex[:10])

    def _path(self, ref: str) -> Path:
        if not _REF_RE.match(ref or ""):
            raise ValueError(f"非法 result ref: {ref!r}")
        return self.base_dir / f"{ref}.txt"

    def save(self, content: str) -> str:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        ref = self._id()
        path = self._path(ref)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        return ref

    def load(self, ref: str, offset: int = 0, length: int = 4000) -> dict:
        if not _REF_RE.match(ref or ""):
            return {"error": f"非法 ref: {ref!r}"}
        path = self._path(ref)
        if not path.exists():
            return {"error": f"未找到结果 {ref}（可能已被清理）"}
        text = path.read_text(encoding="utf-8")
        offset = max(0, int(offset))
        length = max(1, int(length))
        chunk = text[offset:offset + length]
        return {
            "ref": ref,
            "offset": offset,
            "length": len(chunk),
            "total_chars": len(text),
            "has_more": offset + len(chunk) < len(text),
            "content": chunk,
        }

    def clear(self) -> None:
        if self.base_dir.exists():
            for f in self.base_dir.glob("tr_*.txt"):
                try:
                    f.unlink()
                except OSError:
                    pass


_store: Optional[ToolResultStore] = None


def get_result_store() -> ToolResultStore:
    global _store
    if _store is None:
        _store = ToolResultStore()
    return _store


def set_result_store(store: ToolResultStore) -> None:
    global _store
    _store = store
