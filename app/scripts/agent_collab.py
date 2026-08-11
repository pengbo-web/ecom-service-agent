"""协作 worker CLI。

  python -m app.scripts.agent_collab --scan              # 只跑异常扫描并发信号
  python -m app.scripts.agent_collab --once              # 消费一轮
  python -m app.scripts.agent_collab --attribute         # 跑一轮触达转化归因
  python -m app.scripts.agent_collab --followup          # 跑一轮跟进序列自动推进
  python -m app.scripts.agent_collab --loop                 # 常驻(消费 5s / 慢任务 ~100s)
  python -m app.scripts.agent_collab --loop --interval 2 --scan-every 60   # 更快的消费

**常驻模式下两种节奏是分开的**,这是刻意的:

  --interval    消费轮询间隔(默认 5s)。**唯一有延迟意义的一段**——`buyer_hints`
                靠它:买家转人工 → 参谋归因 → 写共享上下文 → 买家下一轮读到提示。
  --scan-every  每 N 轮消费才跑一次扫描/归因/跟进(默认 20,即约 100 秒一次)。
                **限频的理由是钱不是 CPU**:实测四段空转耗时 consume 8.6ms /
                scan 14.8ms / attribute 2.0ms / followup 2.2ms,全跑也才
                332ms/分钟。但 `scan_and_publish` **不去重**,而退款率跨线这类
                异常会持续存在——扫描频率 ×12 = 异常事件 ×12 = 参谋归因的 LLM
                调用 ×12,日预算(默认 200)几分钟就烧穿。

改造前它们绑在同一个 interval 上,于是只有两个选择:整体快(烧穿预算)或整体慢
(牺牲 buyer_hints 新鲜度)。那不是参数没调好,是节奏被耦合了。

拉取式而非常驻监听:买家会话只负责发信号,分析与起草在这里异步跑,
买家那一轮的延迟零增加。归因(--attribute)、跟进序列(--followup)同属这一拨:
都要等窗口期/到期才有事可做,不能挂在买家/店主任何一次同步请求上,只能靠
这个 worker 定期扫。
"""

import argparse
import os
import sys

# 直接按路径跑(`python app/scripts/agent_collab.py --loop`)时,sys.path[0] 是
# app/scripts/,仓库根不在里面,于是 `from app.agent...` 抛 ModuleNotFoundError。
#
# 这不是"用法不对"就能算了的:这个 worker 恰恰是运维在**前端看到「协作 worker
# 已停摆」之后**要手动拉起来的东西,而那一刻最自然的手势就是按路径敲。让它在
# 那一刻炸一个 ModuleNotFoundError,等于把一次本来两秒钟的恢复变成一次排障。
# 三行兜底比一句"请用 -m"可靠。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import sys
import time

#: 心跳记录名。看板据此判"协作 worker 还活着吗、停摆多久了"
#: (见 Database.record_worker_heartbeat 与 /api/admin/collab/health)。
WORKER_NAME = "collab"


def _build_worker_hitl():
    """worker 进程自己的 HITL 视图,供跟进序列复用仲裁(`check_outreach_allowed`)。

    `ManualMode` 是全新的空实例——它本就是内存态,worker 与 API 进程天生不
    共享,这是继承下来的边界(见 app/multi_agent/arbitration.py 顶部注释),
    不是本次改动引入的新缺口。`HandoffQueue` 读的是持久化 SQLite,worker
    这边能看到真实的未结工单,仲裁的"未结工单"这一半判定在这里依然有效。
    """
    from app.config.settings import settings

    if not settings.hitl_enabled:
        return None
    from app.hitl.manager import HitlManager
    from app.hitl.manual_mode import ManualMode
    from app.hitl.queue import HandoffQueue

    hq = HandoffQueue()
    hq.init_schema()
    return HitlManager(hq, ManualMode(settings.manual_mode_timeout),
                       settings.hitl_confidence_threshold)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="多 Agent 协作 worker")
    parser.add_argument("--scan", action="store_true", help="跑一次异常扫描并发信号")
    parser.add_argument("--once", action="store_true", help="消费一轮事件")
    parser.add_argument("--attribute", action="store_true", help="跑一轮触达转化归因")
    parser.add_argument("--followup", action="store_true", help="跑一轮跟进序列自动推进")
    parser.add_argument("--loop", action="store_true", help="常驻循环")
    parser.add_argument("--interval", type=int, default=5,
                        help="消费轮询间隔秒,默认 5(下限 1)")
    parser.add_argument("--scan-every", type=int, default=20, dest="scan_every",
                        help="每 N 轮消费才跑一次扫描/归因/跟进,默认 20"
                             "(配合 --interval 5 = 约 100 秒一次,与改造前 60 秒同量级)")
    parser.add_argument("--window", type=int, default=7, help="扫描窗口天数,默认 7")
    args = parser.parse_args(argv)

    from app.agent.tools.anomaly import scan_and_publish
    from app.multi_agent.collab import run_once
    from app.multi_agent.followup import run_due as run_due_followups
    from app.scripts.attribute_outreach import attribute_once

    if not (args.scan or args.once or args.attribute or args.followup or args.loop):
        parser.print_help()
        return 2

    def _consume_once() -> None:
        """认领并消费一轮。**这是唯一有延迟意义的一段。**

        `buyer_hints` 靠它:买家转人工 → signal.anomaly → 参谋归因 → 写共享上下文
        → 买家**下一轮**读到"该商品近期退换反馈偏多"的提示。这条链上的延迟等于
        本函数的调用间隔,所以它该跑得快。
        """
        stats = run_once()
        # reclaimed 单独打出来:它是"上一个 worker 崩在半路"的唯一可见信号,
        # 混在两段消费统计里会被忽略掉。
        print(f"[consume] reclaimed={stats['reclaimed']} "
              f"analyst={stats['analyst']} growth={stats['growth']}", flush=True)

    def _periodic_once() -> None:
        """扫描 / 归因 / 跟进。**这三件事的时间尺度是分钟到小时级,不该跟着消费跑。**

        **为什么必须限频——理由不是 CPU,是钱。** 实测四段的空转耗时:

            consume 8.6ms | scan 14.8ms | attribute 2.0ms | followup 2.2ms

        也就是说全部每 5 秒跑一遍也只有约 332ms/分钟(0.55% 单核),数据库开销
        完全不是问题。真正的成本在**下游**:`scan_and_publish` 对当前跨线的异常
        **无条件全量 publish、不做去重**,而退款率跨线这类异常会持续存在(阈值不
        会因为你扫过一次就消失)。于是扫描频率提高 12 倍 = 异常事件提高 12 倍 =
        **参谋归因的 LLM 调用提高 12 倍**,`collab_daily_llm_budget`(默认 200)
        会在几分钟内被烧穿,之后归因全部降级为纯统计。

        所以 `--scan-every` 的默认值该跟着**预算**走而不是跟着 CPU 走。
        (若哪天 `scan_and_publish` 加了去重,这条限制可以放宽——但在那之前别
        因为"扫描才 14ms"就把这个参数删掉。)

        另外两件本身也不需要高频:转化归因的窗口是 24 小时,跟进序列的间隔是
        48 小时,几秒内产出必然相同。

        改造前这三件与消费**绑在同一个 interval 上**,于是只有两个选择:整体快
        (烧穿预算)或整体慢(牺牲 buyer_hints 新鲜度)。那不是参数没调好,
        是节奏被耦合了。
        """
        s = scan_and_publish(window_days=args.window)
        print(f"[scan] 异常 {s['anomalies']} 条,发布 {s['published']} 条 "
              f"corr={s['correlation_id']}", flush=True)
        a = attribute_once()
        print(f"[attribute] checked={a['checked']} converted={a['converted']} "
              f"no_change={a['no_change']} "
              # 判不出来的条数也要打出来:它不进转化率分母,不打就等于这些草稿
              # 从统计里消失了(见 attribute_outreach._judge)。
              f"unattributable={a.get('unattributable', 0)}", flush=True)
        f = run_due_followups(hitl=_build_worker_hitl())
        print(f"[followup] checked={f['checked']} drafted={f['drafted']} "
              f"stopped={f['stopped']} done={f['done']}", flush=True)

    def cycle(tick: int = 0) -> None:
        """跑一轮。`tick` 是循环计数,决定这轮要不要带上慢任务。

        **顺序刻意是"先慢后快"**:扫描会 publish 新的 signal.anomaly,紧接着的
        消费就能把它认领掉——同一轮里完成"发现异常 → 参谋归因",而不是等下一轮。
        这与改造前的顺序一致,不是新语义。

        单次模式(`--scan` / `--once` / ...)按显式开关走,与改造前逐字节相同:
        运维用 cron 每分钟调一次 `--once` 是常见部署形态,不能因为循环模式改了
        节奏就把单次语义也一起改掉。
        """
        if args.loop:
            # tick 0 也跑一次慢任务:worker 刚起来时不该等 100 秒才做第一次扫描
            if args.scan_every <= 1 or tick % args.scan_every == 0:
                _periodic_once()
            _consume_once()
            return
        if args.scan:
            s = scan_and_publish(window_days=args.window)
            print(f"[scan] 异常 {s['anomalies']} 条,发布 {s['published']} 条 "
                  f"corr={s['correlation_id']}", flush=True)
        if args.once:
            _consume_once()
        if args.attribute:
            a = attribute_once()
            print(f"[attribute] checked={a['checked']} converted={a['converted']} "
                  f"no_change={a['no_change']} "
                  f"unattributable={a.get('unattributable', 0)}", flush=True)
        if args.followup:
            f = run_due_followups(hitl=_build_worker_hitl())
            print(f"[followup] checked={f['checked']} drafted={f['drafted']} "
                  f"stopped={f['stopped']} done={f['done']}", flush=True)

    def cycle_with_heartbeat(tick: int = 0) -> None:
        """跑一轮并打心跳。

        心跳是"worker 进程整个死了"这件事**唯一**的可观测信号:
        `reclaim_stale_events` 能救"认领后崩在半路"的单条事件,但救不了进程本身
        没了——那种情况下没有任何人去调 reclaim,协作静默停摆,而买家链路一切
        正常、不会有任何症状暴露出来。看板据此判"协作已停摆多久"。

        成功与失败都记:只记成功的话,一个每轮都抛异常的 worker 与一个已经死掉
        的 worker 在看板上长得一模一样(都是 last_success_at 停在过去),而这两
        种故障的处理方式完全不同。
        """
        from app.db import get_db
        try:
            cycle(tick)
        except Exception as exc:  # noqa: BLE001 常驻循环不能被单次异常打断
            print(f"[error] {exc}", flush=True)
            get_db().record_worker_heartbeat(WORKER_NAME, ok=False, error=str(exc))
            return
        get_db().record_worker_heartbeat(WORKER_NAME, ok=True)

    if args.loop:
        # 下限从 5 秒降到 1 秒:改造前的 `max(5, interval)` 意味着传 --interval 3
        # 会被静默钳到 5——一个"传了不生效"的参数比没有这个参数更糟。保留 1 秒
        # 下限是防手滑传 0/负数(那会变成忙等把 CPU 打满)。
        tick = 0
        while True:
            cycle_with_heartbeat(tick)
            tick += 1
            time.sleep(max(1, args.interval))
    else:
        # 单次模式同样打心跳:运维用 cron 每分钟调一次 `--once` 也是常见部署方式,
        # 那种形态下心跳同样是"这个 worker 还活着吗"的唯一依据。
        cycle_with_heartbeat()
    return 0


if __name__ == "__main__":
    sys.exit(main())
