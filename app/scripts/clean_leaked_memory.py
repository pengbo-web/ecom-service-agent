"""清理长期画像里**属于别人的订单信息**(一次性存量清理)。

**为什么需要它**:`app/agent/memory/ownership_filter.py` 那两道过滤(读取侧 + 写入侧)
分别兜住了"念出去"和"再写进来",但**已经躺在磁盘上的数据它们清不掉**。全量审计
`app/sessions/memory/*.json`:66 份画像里 **49 份受影响,越权 fact 1 条、越权摘要 55 条**。

留着它们有三重代价:①过滤器一旦回归/被绕过,泄漏立刻复发;②每一轮都要为这些条目
跑一遍归属查询,纯浪费;③它本身就是不该继续持有的他人数据。

**判据与运行时完全同源**——直接调 `ownership_filter.fact_belongs_to`,不在这里另写
一套。清理脚本和运行时判据分叉,会清出"运行时还拦着、脚本以为干净"这种最糟的状态。

**默认只报告不改文件**(`--apply` 才真写),写之前先整目录备份到
`<memory_dir>/../memory_backup_<时间戳>/`。删记忆是不可逆的,而误删买家自己的记忆
没有任何补救办法。

    python -m app.scripts.clean_leaked_memory              # 干跑,只报告
    python -m app.scripts.clean_leaked_memory --apply      # 备份后真清
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


def scan_file(path: Path) -> dict:
    """审一份画像,返回 {user_id, bad_facts, bad_summaries, total_*}。不改文件。"""
    from app.agent.memory.ownership_filter import fact_belongs_to

    data = json.loads(path.read_text(encoding="utf-8"))
    uid = str(data.get("user_id") or "")

    facts = data.get("facts") or []
    summaries = data.get("interaction_summaries") or []
    bad_facts = [f for f in facts
                 if not fact_belongs_to(str(f.get("content") or ""), uid)]
    bad_summaries = [s for s in summaries
                     if not fact_belongs_to(str(s.get("summary") or ""), uid)]

    return {"path": path, "user_id": uid, "data": data,
            "facts": facts, "summaries": summaries,
            "bad_facts": bad_facts, "bad_summaries": bad_summaries}


def clean_file(report: dict) -> None:
    """把审出来的越权条目从画像里去掉并写回(调用方负责已经备份过)。"""
    data = report["data"]
    bad_f = {id(f) for f in report["bad_facts"]}
    bad_s = {id(s) for s in report["bad_summaries"]}
    data["facts"] = [f for f in report["facts"] if id(f) not in bad_f]
    data["interaction_summaries"] = [s for s in report["summaries"] if id(s) not in bad_s]

    # 原子写:清理过程中被打断不能留下半个画像文件(与 app/agent/storage.py 同姿态)
    tmp = report["path"].with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(report["path"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", default="app/sessions/memory")
    parser.add_argument("--apply", action="store_true",
                        help="真的写文件(默认只报告)。会先整目录备份。")
    args = parser.parse_args(argv)

    from app.config.settings import settings
    if not getattr(settings, "auth_enabled", False):
        # 与 owned_order / filter_owned 同一门控:没有"归属"这回事时不该清任何东西。
        print("auth_enabled=False:单机教学模式没有归属概念,不做清理。")
        return 0

    mem_dir = Path(args.memory_dir)
    if not mem_dir.is_dir():
        print(f"目录不存在: {mem_dir}")
        return 1

    reports, failed = [], []
    for path in sorted(mem_dir.glob("*.json")):
        try:
            reports.append(scan_file(path))
        except Exception as exc:  # noqa: BLE001 读不出的单独列出,不静默跳过
            failed.append((path.name, str(exc)))

    dirty = [r for r in reports if r["bad_facts"] or r["bad_summaries"]]
    tot_f = sum(len(r["bad_facts"]) for r in dirty)
    tot_s = sum(len(r["bad_summaries"]) for r in dirty)

    print(f"扫描 {len(reports)} 份画像,{len(dirty)} 份含他人订单信息"
          f"(越权事实 {tot_f} 条、越权摘要 {tot_s} 条)")
    for r in dirty[:10]:
        print(f"  {r['path'].name:<24} user={r['user_id']:<10} "
              f"fact={len(r['bad_facts'])} summary={len(r['bad_summaries'])}")
    if len(dirty) > 10:
        print(f"  …另有 {len(dirty) - 10} 份")
    if failed:
        print(f"读不出的文件 {len(failed)} 份(未处理): {failed[:5]}")

    if not args.apply:
        print("\n干跑,没有改动任何文件。加 --apply 才会真清(会先备份)。")
        return 0
    if not dirty:
        print("没有要清的。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = mem_dir.parent / f"memory_backup_{stamp}"
    shutil.copytree(mem_dir, backup)
    print(f"\n已备份整个目录到: {backup}")

    for r in dirty:
        clean_file(r)
    print(f"已清理 {len(dirty)} 份画像,移除越权事实 {tot_f} 条、越权摘要 {tot_s} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
