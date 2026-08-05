"""协作 worker CLI。

  python -m app.scripts.agent_collab --scan              # 只跑异常扫描并发信号
  python -m app.scripts.agent_collab --once              # 消费一轮
  python -m app.scripts.agent_collab --attribute         # 跑一轮触达转化归因
  python -m app.scripts.agent_collab --loop --interval 60  # 常驻

拉取式而非常驻监听:买家会话只负责发信号,分析与起草在这里异步跑,
买家那一轮的延迟零增加。归因(--attribute)同属这一拨:发送后要等窗口期,
不能挂在买家/店主任何一次同步请求上,只能靠这个 worker 定期扫。
"""

import argparse
import sys
import time


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="多 Agent 协作 worker")
    parser.add_argument("--scan", action="store_true", help="跑一次异常扫描并发信号")
    parser.add_argument("--once", action="store_true", help="消费一轮事件")
    parser.add_argument("--attribute", action="store_true", help="跑一轮触达转化归因")
    parser.add_argument("--loop", action="store_true", help="常驻循环")
    parser.add_argument("--interval", type=int, default=60, help="循环间隔秒,默认 60")
    parser.add_argument("--window", type=int, default=7, help="扫描窗口天数,默认 7")
    args = parser.parse_args(argv)

    from app.agent.tools.anomaly import scan_and_publish
    from app.multi_agent.collab import run_once
    from app.scripts.attribute_outreach import attribute_once

    if not (args.scan or args.once or args.attribute or args.loop):
        parser.print_help()
        return 2

    def cycle() -> None:
        if args.scan or args.loop:
            s = scan_and_publish(window_days=args.window)
            print(f"[scan] 异常 {s['anomalies']} 条,发布 {s['published']} 条 "
                  f"corr={s['correlation_id']}", flush=True)
        if args.once or args.loop:
            stats = run_once()
            # reclaimed 单独打出来:它是"上一个 worker 崩在半路"的唯一可见信号,
            # 混在两段消费统计里会被忽略掉。
            print(f"[consume] reclaimed={stats['reclaimed']} "
                  f"analyst={stats['analyst']} growth={stats['growth']}", flush=True)
        if args.attribute or args.loop:
            a = attribute_once()
            print(f"[attribute] checked={a['checked']} converted={a['converted']} "
                  f"no_change={a['no_change']}", flush=True)

    if args.loop:
        while True:
            try:
                cycle()
            except Exception as exc:  # noqa: BLE001 常驻循环不能被单次异常打断
                print(f"[error] {exc}", flush=True)
            time.sleep(max(5, args.interval))
    else:
        cycle()
    return 0


if __name__ == "__main__":
    sys.exit(main())
