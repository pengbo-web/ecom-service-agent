"""回流用例合并进回归集:按 id 去重,**永不覆盖**已有用例。

人工维护的用例带着人写的期望,是资产;回流用例是机器从线上 trace 生成的推测。
后者不该覆盖前者——否则一次回流就能把人调好的期望冲掉。
"""

from __future__ import annotations


def merge_cases(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int]:
    """返回 (合并后列表, 新增条数)。已存在的 id 原样保留,不合并字段。"""
    seen = {str(c.get("id")) for c in existing if c.get("id")}
    merged = list(existing)
    added = 0
    for c in incoming:
        cid = str(c.get("id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        merged.append(c)
        added += 1
    return merged, added
