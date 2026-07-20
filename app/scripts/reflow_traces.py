"""把线上问题 Trace（出错/被拦截/转人工）回流成评估用例。

用法：python -m app.scripts.reflow_traces [--limit 200] [--out app/sessions/reflow_cases.json]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.observability.store import TraceStore  # noqa: E402
from app.evaluation.trace_to_case import trace_to_case, is_problem_trace  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="线上问题 Trace 回流成评估用例")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default="app/sessions/reflow_cases.json")
    args = ap.parse_args()

    store = TraceStore()
    recent = store.recent_traces(limit=args.limit)
    cases = []
    for row in recent:
        full = store.get_trace(row["trace_id"])
        if full and is_problem_trace(full):
            cases.append(trace_to_case(full))

    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"回流 {len(cases)} 条问题用例 → {out}")


if __name__ == "__main__":
    main()
