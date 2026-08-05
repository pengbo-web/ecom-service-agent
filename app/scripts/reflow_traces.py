"""把线上问题 Trace（出错/被拦截/转人工）回流成评估用例。

用法：python -m app.scripts.reflow_traces [--limit 200] [--out app/sessions/reflow_cases.json]
     [--merge-into app/evaluation/cases.json]

--merge-into 可选：不传时行为与之前完全一致（只写独立文件，不接 Database、不
带 expected_keywords）。传了则改用带人工回复关键词的一次 collect_reflow_cases
结果——同时写 --out 与合并进目标回归集文件（不覆盖已有的人工维护用例），后者
原子写回。
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.observability.store import TraceStore  # noqa: E402
from app.evaluation.trace_to_case import collect_reflow_cases  # noqa: E402
from app.evaluation.case_merge import merge_cases  # noqa: E402


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写回:先写临时文件再 replace,避免半截写坏已有的检入资产。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def main():
    ap = argparse.ArgumentParser(description="线上问题 Trace 回流成评估用例")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default="app/sessions/reflow_cases.json")
    ap.add_argument("--merge-into", default=None,
                    help="额外把回流用例合并进该回归集文件(不覆盖已有用例);不传则不合并")
    args = ap.parse_args()

    store = TraceStore()
    if args.merge_into:
        # 只有走合并这条路径才接 Database、补人工回复关键词;--out 与
        # --merge-into 共用同一份结果,collect_reflow_cases 只对 store 扫一遍,
        # 不再各扫一次(改造前是两次,一次带 db 一次不带,重复扫了同一批 trace)。
        from app.db import get_db
        cases = collect_reflow_cases(store, limit=args.limit, db=get_db())
    else:
        # 不带 db 的原始调用:与改造前完全同路径,保证 --merge-into 缺省时字节级不变。
        cases = collect_reflow_cases(store, limit=args.limit)

    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"回流 {len(cases)} 条问题用例 → {out}")

    if args.merge_into:
        target = ROOT / args.merge_into if not Path(args.merge_into).is_absolute() \
            else Path(args.merge_into)
        existing_cases = []
        if target.exists():
            existing_cases = json.loads(target.read_text(encoding="utf-8")).get("cases", [])
        merged, added = merge_cases(existing_cases, cases)
        _atomic_write_json(target, {"cases": merged})
        print(f"合并进回归集 {target}: 新增 {added} 条(共 {len(merged)} 条)")


if __name__ == "__main__":
    main()
