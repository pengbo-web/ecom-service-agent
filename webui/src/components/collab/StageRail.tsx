/**
 * 协作链的**阶段轨**:把"这条链走到了哪一步、卡在哪"变成一眼能看懂的东西。
 *
 * **为什么需要它**(用户反馈"看不清 agent 的流转过程"):改造前列表上每条链只显示
 * 「客服 Agent → 参谋 Agent · 1 个事件」。而实测 30 条链里有 28 条都长这样——
 * 因为协作 worker 停摆,它们全部卡在第一步。页面上**看不出这是"刚起头"还是
 * "已经走完"**,更看不出"卡住了"。健康条虽然写了 worker 已停摆,但没人会把那句话
 * 和"所以每条链都只有 1 个事件"联系起来。
 *
 * 阶段序取自总线的规范链(app/multi_agent/routing.py 的 SUBSCRIPTIONS):
 *
 *     signal.anomaly → insight.diagnosis → action.drafts_ready
 *                    → result.outreach_sent → result.outreach_*
 *
 * **不是每条链都会走完全部五步,而"没走完"往往是对的**:归因降级的诊断按规则
 * 不唤醒营销(routing._marketing_worthy),这条链正常地停在第 2 步。所以未到达的
 * 阶段渲染成**空心**而不是红色——空心是"还没轮到/不该轮到",不是故障。真正的
 * 故障只有 failed 一种,单独用红色。
 */
import { CheckCircle2, Circle, CircleDashed, MinusCircle, XCircle } from "lucide-react";

/** 一步的状态。`missing` = 这一步的事件根本没出现过(未到达或按规则跳过整段)。 */
export type StageState = "done" | "active" | "failed" | "skipped" | "missing";

export type Stage = {
  key: string;          // 事件类型前缀(result.outreach_converted/no_change 合并成一步)
  label: string;        // 给人看的步骤名
  actor: string;        // 这一步**由谁产出**
  state: StageState;
  /** pending 时**在等谁处理**(该事件的 target_agent)。与 actor 是两码事。 */
  waitingOn?: string;
};

/** 规范链的五步。顺序即时间序,**唯一口径**,别处不许再抄一份。 */
const CANON: { match: (t: string) => boolean; label: string; actor: string }[] = [
  { match: (t) => t === "signal.anomaly",       label: "发现异常",   actor: "客服 / 扫描器" },
  { match: (t) => t === "insight.diagnosis",    label: "参谋归因",   actor: "参谋 Agent" },
  { match: (t) => t === "action.drafts_ready",  label: "营销起草",   actor: "营销 Agent" },
  { match: (t) => t === "result.outreach_sent", label: "人工审批发出", actor: "人工" },
  { match: (t) => t.startsWith("result.outreach_conv") || t.startsWith("result.outreach_no"),
    label: "转化归因", actor: "归因 worker" },
];

/**
 * 把 (事件类型[], 状态[]) 还原成五步进度。
 *
 * 同一步可能有多行(扇出/重复扫描),按**最坏状态优先**归并:
 * failed > pending/processing(active) > skipped > done。理由是这个视图要回答的是
 * "有没有卡住",一步里只要有一条还没走完,这一步就不算走完——把它显示成 done
 * 会让人以为可以往下看了。
 */
const AGENT_CN: Record<string, string> = {
  analyst: "参谋 Agent", growth: "营销 Agent", human: "人工", service: "客服 Agent",
};

export function deriveStages(eventTypes?: string[], statuses?: string[],
                             targets?: string[]): Stage[] {
  const types = eventTypes || [];
  const sts = statuses || [];
  const tgs = targets || [];
  return CANON.map((c) => {
    let state: StageState = "missing";
    let waitingOn: string | undefined;
    const rank: Record<StageState, number> = {
      failed: 4, active: 3, skipped: 2, done: 1, missing: 0,
    };
    types.forEach((t, i) => {
      if (!c.match(t)) return;
      const raw = sts[i] || "";
      const s: StageState =
        raw === "failed" ? "failed"
        : raw === "pending" || raw === "processing" ? "active"
        : raw === "skipped" ? "skipped"
        : "done";
      if (rank[s] > rank[state]) {
        state = s;
        // 只有 pending/processing 才谈得上"在等谁":事件已产出、收件人还没处理。
        waitingOn = s === "active" ? (AGENT_CN[tgs[i]] || tgs[i] || "") : undefined;
      }
    });
    return { key: c.label, label: c.label, actor: c.actor, state, waitingOn };
  });
}

/**
 * 一句话说清这条链现在什么处境。列表上每条都要有——徽章能表达状态,
 * 表达不了"所以现在等谁"。
 */
export function stageSummary(stages: Stage[]): { text: string; tone: "bad" | "wait" | "ok" | "unknown" } {
  // **"拿不到进度"必须与"还没开始"分开。** 后端老版本不返回 event_types 时,
  // 全部阶段都是 missing——把这种情况说成"尚未开始"就是在编造一个它并不知道的
  // 事实(实测踩过:API 进程跑的是加字段之前的代码,页面上 30 条链齐刷刷写着
  // "尚未开始",而它们其实都有待处理事件)。
  if (stages.every((s) => s.state === "missing")) {
    return { text: "进度未知", tone: "unknown" };
  }
  const failed = stages.find((s) => s.state === "failed");
  if (failed) return { text: `失败:${failed.label}`, tone: "bad" };
  const active = stages.find((s) => s.state === "active");
  if (active) {
    // **措辞方向**:pending 表示这一步的事件**已经产出**,只是收件人还没处理。
    // 早先写成"等待:转化归因(归因 worker)",而那 41 条 result.outreach_converted
    // 正是归因 worker 产出的、在等人看——把"等人处理"说成"等它产出",方向反了。
    const who = active.waitingOn ? `等 ${active.waitingOn} 处理` : "等待处理";
    return { text: `${who}:${active.label}`, tone: "wait" };
  }
  const skipped = stages.find((s) => s.state === "skipped");
  if (skipped) return { text: `已拦下:${skipped.label}`, tone: "wait" };
  const doneCount = stages.filter((s) => s.state === "done").length;
  if (doneCount === 0) return { text: "尚未开始", tone: "wait" };
  const lastDone = [...stages].reverse().find((s) => s.state === "done");
  // 走到第 5 步才算整条闭环;停在中间不一定是问题(降级诊断不转营销就正常停在第 2 步),
  // 所以措辞是"止于",不是"卡在"——后者会把正常终止说成故障。
  if (stages[stages.length - 1].state === "done") return { text: "已闭环", tone: "ok" };
  return { text: `止于:${lastDone?.label ?? "开始"}`, tone: "ok" };
}

const ICON: Record<StageState, typeof Circle> = {
  done: CheckCircle2, active: CircleDashed, failed: XCircle,
  skipped: MinusCircle, missing: Circle,
};

const TONE: Record<StageState, string> = {
  done: "text-emerald-600 dark:text-emerald-400",
  active: "text-amber-600 dark:text-amber-400",
  failed: "text-destructive",
  skipped: "text-muted-foreground",
  // 未到达用最弱的灰:它**不是故障**,只是还没轮到(或按规则不该轮到)。
  missing: "text-muted-foreground/35",
};

/** 紧凑轨:列表里每条链一行。只有图标与连线,不占地方。 */
export function StageRailCompact({ stages }: { stages: Stage[] }) {
  return (
    <div className="flex items-center gap-0.5" aria-hidden="true">
      {stages.map((s, i) => {
        const Icon = ICON[s.state];
        return (
          <span key={s.key} className="flex items-center gap-0.5">
            {i > 0 && (
              <span className={`h-px w-2.5 ${
                stages[i - 1].state === "done" ? "bg-emerald-600/40" : "bg-border"}`} />
            )}
            <Icon className={`h-3 w-3 ${TONE[s.state]}`} />
          </span>
        );
      })}
    </div>
  );
}

/** 完整轨:详情面板用,带步骤名与执行者。 */
export function StageRail({ stages }: { stages: Stage[] }) {
  return (
    // 横向可滚:五步在窄屏放不下,**让轨道自己滚,页面永远不横滚**
    <div className="overflow-x-auto">
      <ol className="flex min-w-max items-start gap-0">
        {stages.map((s, i) => {
          const Icon = ICON[s.state];
          return (
            <li key={s.key} className="flex items-start">
              {i > 0 && (
                <span className={`mt-3 h-px w-6 shrink-0 sm:w-10 ${
                  stages[i - 1].state === "done" ? "bg-emerald-600/40" : "bg-border"}`} />
              )}
              <div className="flex w-[104px] flex-col items-center gap-1 px-1 text-center">
                <Icon className={`h-5 w-5 ${TONE[s.state]}`} />
                <span className={`text-[11px] leading-tight ${
                  s.state === "missing" ? "text-muted-foreground/50" : "font-medium"}`}>
                  {s.label}
                </span>
                <span className="text-[10px] leading-tight text-muted-foreground">{s.actor}</span>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
