"""评估回归门禁：与 baseline 对比，掉点超过容差即判回退。"""

import json
from pathlib import Path
from typing import Optional

_METRICS = ["pass_rate", "avg_process_score", "avg_result_score"]


def compare_to_baseline(current: dict, baseline: dict, tolerance: float = 0.05) -> dict:
    diffs = []
    regressed = False
    for m in _METRICS:
        b = baseline.get(m)
        c = current.get(m)
        if b is None or c is None:
            continue
        delta = c - b
        m_reg = delta < -tolerance
        if m_reg:
            regressed = True
        diffs.append({"metric": m, "baseline": b, "current": c,
                      "delta": delta, "regressed": m_reg})
    return {"regressed": regressed, "diffs": diffs}


def save_baseline(summary: dict, path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def load_baseline(path) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
