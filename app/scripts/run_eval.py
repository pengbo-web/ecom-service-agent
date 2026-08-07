"""离线运行 Agent 评估体系（第9期）。

用法：
  # 先跑确定性路径（只用代码规则，快、零额外 LLM 开销），验证沙箱与采集
  python app/scripts/run_eval.py --no-judge

  # 全量评估（含 LLM-as-judge：回答质量 / 幻觉 / 过程合理性）
  python app/scripts/run_eval.py

  # 评估 Multi-Agent 编排器，并把报告写到文件
  python app/scripts/run_eval.py --mode multi --output app/sessions/eval_report.json

流程：
  1. 加载数据集（cases.json）。
  2. 构建 Sandbox（隔离 session + 关记忆读 + 本地 mock 工具 + 给共享 client 插桩）。
  3. Evaluator 逐用例：沙箱跑用例 → 过程层 + 结果层双层评分。
  4. 打印双层报告表格 + token 汇总，可选写入 JSON。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from app.utils.console import enable_utf8_stdout  # noqa: E402
from app.evaluation.dataset import load_dataset  # noqa: E402
from app.evaluation.evaluator import Evaluator  # noqa: E402
from app.evaluation.regression import (  # noqa: E402
    compare_to_baseline, save_baseline, load_baseline,
)
from app.evaluation.sandbox import Sandbox  # noqa: E402
from app.observability.langfuse_bridge import background_trace  # noqa: E402
from app.observability.langfuse_client import make_openai_client  # noqa: E402


def _fmt(value) -> str:
    """格式化评分单元格：None→"-"，bool→✓/✗，float→百分比。"""
    if value is None:
        return "  - "
    if isinstance(value, bool):
        return "  ✓ " if value else "  ✗ "
    return f"{value * 100:4.0f}%"


def _print_report(report: dict) -> None:
    cases = report["cases"]

    print("\n" + "=" * 78)
    print("  过程指标（工具准确率 / 调用效率 / token达标 / 过程合理性）")
    print("=" * 78)
    print(f"  {'用例':<22}{'工具准确':>8}{'调用效率':>8}{'token':>8}{'token达标':>9}{'过程合理':>8}")
    for c in cases:
        p = c["process"]
        print(
            f"  {c['case_id']:<22}"
            f"{_fmt(p['tool_accuracy']):>8}{_fmt(p['tool_efficiency']):>8}"
            f"{p['token_cost']:>8}{_fmt(p['token_pass']):>9}{_fmt(p['process_soundness']):>8}"
        )

    print("\n" + "=" * 78)
    print("  结果指标（意图 / 关键信息完整性 / 转人工 / 回答质量 / 忠实度无幻觉）")
    print("=" * 78)
    print(f"  {'用例':<22}{'意图':>8}{'完整性':>8}{'转人工':>8}{'回答质量':>8}{'无幻觉':>8}")
    for c in cases:
        r = c["result"]
        flag = "" if c["passed"] else "  ❌"
        if c["error"]:
            print(f"  {c['case_id']:<22}  运行/评分异常: {c['error']}")
            continue
        print(
            f"  {c['case_id']:<22}"
            f"{_fmt(r['intent_match']):>8}{_fmt(r['keyword_coverage']):>8}"
            f"{_fmt(r['requires_human_match']):>8}{_fmt(r['answer_quality']):>8}"
            f"{_fmt(r['faithfulness']):>8}{flag}"
        )

    s = report["summary"]
    print("\n" + "=" * 78)
    print("  汇总")
    print("=" * 78)
    print(f"  通过率        : {s['passed']}/{s['total']}  ({s['pass_rate'] * 100:.0f}%)")
    print(f"  平均过程得分  : {_fmt(s['avg_process_score']).strip()}")
    print(f"  平均结果得分  : {_fmt(s['avg_result_score']).strip()}")
    print(f"  总 token 消耗 : {s['total_tokens']}（平均每用例 {s['avg_tokens_per_case']:.0f}）")


def main():
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="运行 Agent 评估体系")
    parser.add_argument(
        "--dataset", default=settings.eval_dataset_path,
        help=f"测试集 JSON 路径（默认: {settings.eval_dataset_path}）",
    )
    parser.add_argument(
        "--mode", choices=["single", "multi"],
        default="multi",   # H1.0-C:总控 Agent 唯一入口,默认 multi
        help="被测 Agent 模式（默认 multi;single 已废弃）",
    )
    parser.add_argument(
        "--judge", dest="judge", action="store_true", default=settings.eval_use_judge,
        help="启用 LLM-as-judge（默认开）",
    )
    parser.add_argument(
        "--no-judge", dest="judge", action="store_false",
        help="只跑代码规则指标，不调 LLM judge（快、便宜）",
    )
    parser.add_argument("--output", default=None, help="将完整报告写入 JSON 文件")
    parser.add_argument("--save-baseline", action="store_true",
                        help="将本次评估 summary 存为回归基线")
    parser.add_argument("--regression", action="store_true",
                        help="与基线对比，掉点超过容差则以非零码退出（CI 门禁）")
    args = parser.parse_args()

    dataset_path = ROOT / args.dataset if not Path(args.dataset).is_absolute() else Path(args.dataset)

    print("=" * 78)
    print("  并夕夕 · Agent 评估体系")
    print(f"  模式      : {args.mode}")
    print(f"  数据集    : {dataset_path}")
    print(f"  LLM judge : {'开启' if args.judge else '关闭（仅规则）'}")
    print(f"  裁判模型  : {settings.model_name}")
    print("=" * 78)

    print("\n[1/3] 加载测试集...")
    if not dataset_path.exists():
        print(f"❌ 测试集不存在: {dataset_path}")
        sys.exit(1)
    cases = load_dataset(dataset_path)
    if not cases:
        print("❌ 测试集为空")
        sys.exit(1)
    print(f"   共 {len(cases)} 条用例")

    print(f"\n[2/3] 在沙箱中重跑测试集（{args.mode} 模式）...")
    client = make_openai_client(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    sandbox = Sandbox(mode=args.mode)
    evaluator = Evaluator(
        sandbox=sandbox,
        client=client,
        model=settings.model_name,
        use_judge=args.judge,
        pass_threshold=settings.eval_pass_threshold,
    )
    # 阶段一 gap⑤:离线评估这一次真调 LLM 的入口包进命名 trace,便于在
    # Langfuse 里看到整套用例重跑的耗时与内部各次生成。
    with background_trace("run_eval", input={"mode": args.mode, "cases": len(cases)}):
        report = evaluator.run_all(cases)

    print("\n[3/3] 生成评估报告...")
    _print_report(report)

    baseline_path = ROOT / settings.eval_baseline_path
    if args.save_baseline:
        save_baseline(report["summary"], baseline_path)
        print(f"\n✅ 已保存回归基线: {baseline_path}")

    if args.regression:
        baseline = load_baseline(baseline_path)
        if baseline is None:
            print(f"\n⚠️  无基线可比（先跑 --save-baseline）: {baseline_path}")
        else:
            cmp = compare_to_baseline(report["summary"], baseline,
                                      settings.eval_regression_tolerance)
            print("\n" + "=" * 78)
            print("  回归门禁（vs 基线）")
            print("=" * 78)
            for d in cmp["diffs"]:
                flag = "❌ 回退" if d["regressed"] else "✅"
                print(f"  {d['metric']:<20} 基线 {d['baseline']:.3f} → 本次 "
                      f"{d['current']:.3f}  (Δ {d['delta']:+.3f}) {flag}")
            if cmp["regressed"]:
                print("\n❌ 检测到质量回退，评估门禁未通过。")
                sys.exit(1)
            print("\n✅ 未见回退，评估门禁通过。")

    if args.output:
        out_path = ROOT / args.output if not Path(args.output).is_absolute() else Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n   报告已写入: {out_path}")

    print("\n🎉 评估完成。")


if __name__ == "__main__":
    main()
