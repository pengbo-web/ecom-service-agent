"""业务效果报表 CLI:`python -m app.scripts.business_report [--days 7]`。

与 `app.scripts.eval_cli`(沙箱能力评测)分开的理由见
`app/evaluation/business_metrics.py` 顶部:两者回答不同的问题,共用一个入口会让
"沙箱 10 条用例"和"线上几百轮真实对话"看起来像同一份数据。
"""

from __future__ import annotations

import argparse
import json


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def _num(x, fmt: str = "%.1f") -> str:
    return "—" if x is None else fmt % x


def _print(report: dict) -> None:
    t, s, sat = report["turn_level"], report["session_level"], report["satisfaction"]
    ta = report["all_sources"]["turn_level"]
    win = report["window_days"]
    print("=" * 66)
    print("业务效果报表  窗口=%s  判定口径=%s"
          % ("全时段" if not win else f"近 {win} 天",
             "/".join(report["decision_sources"])))
    print("=" * 66)
    print("\n【真实流量】轮级 %d 轮" % t["turns"])
    print("  自助完成率 %s   工具失败率 %s   转人工率 %s"
          % (_pct(t["self_service_rate"]), _pct(t["tool_error_rate"]),
             _pct(t["escalation_rate"])))
    print("\n【真实流量】会话级 %d 通" % s["sessions"])
    print("  自助解决率 %s   会话级转人工率 %s   平均轮次 %s"
          % (_pct(s["containment_rate"]), _pct(s["session_escalation_rate"]),
             _num(s["avg_turns_per_session"])))
    print("\n【满意度】评价 %d 条,差评 %d 条,差评率 %s%s"
          % (sat["reviews"], sat["bad_reviews"], _pct(sat["bad_review_rate"]),
             "" if sat["source_filtered"] else "（该项无法按流量来源过滤）"))

    print("\n" + "-" * 66)
    print("【全量对照】不按来源过滤会得出什么 —— 轮级 %d 轮" % ta["turns"])
    print("  自助完成率 %s   工具失败率 %s   转人工率 %s"
          % (_pct(ta["self_service_rate"]), _pct(ta["tool_error_rate"]),
             _pct(ta["escalation_rate"])))
    print("\n【失真幅度】live 减全量,正数=全量偏低")
    for scope, block in report["distortion"].items():
        for k, v in block.items():
            if v["delta"] is None:
                continue
            print("  %-8s %-22s live=%-8s 全量=%-8s Δ=%+.4f"
                  % (scope, k, v["live"], v["all_sources"], v["delta"]))

    if report["insufficient"]:
        print("\n【样本不足,未给出比率】")
        for x in report["insufficient"]:
            print("  %s:%d 条 < 下限 %d → %s"
                  % (x["scope"], x["samples"], x["min_samples"],
                     "/".join(x["metrics"])))
    if not report["cost"]["available"]:
        print("\n【成本】不可用:%s" % report["cost"]["reason"])


def main() -> int:
    ap = argparse.ArgumentParser(description="真实流量上的客服业务效果报表")
    ap.add_argument("--days", type=int, default=None, help="统计窗口天数;省略=全时段")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = ap.parse_args()

    from app.evaluation.business_metrics import business_report
    report = business_report(window_days=args.days)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
