"""WS5 多轮用户模拟跑批入口(技术方案 §7)。

用法:
  python -m app.scripts.run_user_sim --list
  python -m app.scripts.run_user_sim --persona policy_pushback
  python -m app.scripts.run_user_sim                 # 跑全部 persona

**口径提醒(每次跑都打印)**:模拟流量标 SOURCE_SIMULATED,DECISION 与
SAMPLING 两套口径都排除——它只服务评测与回归,永不进语料采样、永不进看门狗
判定。拿模拟对话合成门禁用例=闭环自证(runtime_context.py:129-133)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation import user_simulator  # noqa: E402
from app.utils.console import enable_utf8_stdout  # noqa: E402

_CALIBER_NOTE = ("口径:以上流量标 SOURCE_SIMULATED,判定与采样两套口径都排除;"
                 "只服务评测与回归,永不进语料采样/看门狗判定。")


def main(argv=None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="多轮用户模拟(scripted persona 原型)")
    parser.add_argument("--list", action="store_true", help="列出 persona")
    parser.add_argument("--persona", default=None, help="只跑一个 persona id")
    args = parser.parse_args(argv)

    personas = user_simulator.load_personas()
    if args.list:
        for p in personas:
            print(f"{p.id:<22} opener={p.opener!r} followups={len(p.followups)}"
                  f" goal={p.goal_keywords or '无(跑到脚本尽/耐心尽)'}")
        return 0
    if args.persona:
        personas = [p for p in personas if p.id == args.persona]
        if not personas:
            print(f"未找到 persona: {args.persona}")
            return 1

    for p in personas:
        result = user_simulator.run_in_sandbox(p)
        print(f"\n== persona {p.id} · {result.n_turns} 轮 · 停止={result.stop_reason}")
        for t in result.turns:
            print(f"  买家: {t.user}")
            print(f"  小夕: {t.reply[:160]}")
    print("\n" + _CALIBER_NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
