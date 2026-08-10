import { Card } from "@/components/ui/card";

export type Metrics = {
  total_traces: number; error_rate: number; latency_p50_ms: number; latency_p95_ms: number;
  tool_success_rate: number; tool_calls: number; guard_blocks: number; block_rate: number;
  guard_sanitizes: number; handoffs: number; escalation_rate: number;
  // KB 召回降级:本项目最后一条曾经没有出口的降级路径。实测 ApeRAG 停了 24 分钟
  // 无人发现——客服照常回答,只是答案里没有任何政策依据。三个数分开报,因为处置
  // 完全不同:degraded 去修依赖 / miss 可能要补文档 / skipped 是正常。
  kb_recall_attempts?: number; kb_recall_degraded?: number;
  kb_degraded_rate?: number; kb_recall_skipped?: number;
  // 首字时间:总延迟是"整段回复生成完",首字是"买家多久看到第一个字"——流式下
  // 总时长 12s 但首字 1.5s 可接受,首字 12s 是灾难。在线客服的核心 KPI 是后者。
  //
  // ttft_p50_ms 是**买家侧**(第一个字真的进了 SSE 队列)。引擎侧单独报一份:两者
  // 之差是增量脱敏 holdback 的体感代价。这个差曾经大到让引擎侧的数失去意义——
  // 扣留量按最宽护栏正则取 114 字符时,75 字的回复一条 delta 都发不出去,引擎侧
  // 首字一秒多而买家是等到最后一次性看到全文。差值变大 = 有人加了更宽的护栏。
  ttft_p50_ms?: number; ttft_p95_ms?: number; streamed_traces?: number;
  ttft_engine_p50_ms?: number; ttft_holdback_cost_p50_ms?: number;
  ttft_holdback_paired?: number;   // 代价是按 trace 配对算的,这是配对样本数
  total_prompt_tokens: number; total_completion_tokens: number; est_cost_usd: number;
  intent_distribution: Record<string, number>;
  // 统计窗口(小时)。null/缺失 = 全部历史。口径必须跟着数字一起显示:
  // 一个百分比脱离了统计窗口就没有意义。
  window_hours?: number | null;
};

const pct = (x: number) => (x * 100).toFixed(1) + "%";
const ms = (x: number) => (x >= 1000 ? (x / 1000).toFixed(1) + " s" : x.toFixed(0) + " ms");
const num = (x: number) => x.toLocaleString();

/** 一条参考线:超过 warn 变黄,超过 bad 变红(`lowerIsBetter=false` 时方向相反)。 */
type Line = { warn: number; bad: number; lowerIsBetter?: boolean; note: string };

/** 参考目标线。
 *
 * **这些是展示侧的判读参考,不是后端策略,也不是对外承诺的 SLA。** 单独写在这里
 * 并且每条都带一句 `note` 说明依据,是为了让看的人知道"凭什么说 11.5s 是红的"——
 * 一个不敢说出判据的阈值,和没有阈值一样不可信。
 *
 * 之所以必须有:改造前 14 张卡片颜色完全一致,`延迟 P50 = 11516 ms` 和
 * `总请求数 = 473` 在视觉上没有任何区别。而 11.5 秒的中位延迟对在线客服是
 * 灾难级的——运营盯着看板也看不出这里出了事。指标没有参考线就只是数字。
 */
const LINES: Record<string, Line> = {
  latency_p50: { warn: 3000, bad: 6000, lowerIsBetter: true,
    note: "在线客服的中位首响一般要求 3 秒内;超过 6 秒买家会重复发问或直接走掉" },
  latency_p95: { warn: 10000, bad: 20000, lowerIsBetter: true,
    note: "长尾请求(多轮工具调用)可放宽,但 20 秒以上基本等同于没有回应" },
  error_rate: { warn: 0.01, bad: 0.05, lowerIsBetter: true,
    note: "每 100 次对话失败 1 次即需排查;5% 属于线上故障级" },
  tool_success_rate: { warn: 0.95, bad: 0.9, lowerIsBetter: false,
    note: "工具失败会让 Agent 靠猜作答;低于 90% 时回答可信度已不可控" },
  escalation_rate: { warn: 0.15, bad: 0.3, lowerIsBetter: true,
    note: "转人工率是 AI 顶不顶得住的直接体现;偏高说明知识或能力有缺口" },
  ttft_p50: { warn: 1500, bad: 3000, lowerIsBetter: true,
    note: "买家从发送到看到第一个字的时间(买家侧口径,已计入增量脱敏缓冲)。"
        + "这是在线客服真正的体感指标——总延迟 12s 但首字 1.5s 是可接受的,"
        + "首字 12s 则等同于没有回应" },
  ttft_holdback_cost: { warn: 300, bad: 1000, lowerIsBetter: true,
    note: "买家首字与引擎首字之差 = 流式脱敏为了保证「不泄漏」而扣住尾部不发的代价。"
        + "护栏正则越宽扣得越久;这个数变大通常意味着有人加了一条更宽的模式,"
        + "而那件事在别处没有任何信号" },
  kb_degraded_rate: { warn: 0.02, bad: 0.1, lowerIsBetter: true,
    note: "知识库连不上时客服会照常回答、但答案里没有任何政策依据(退货运费之类答的是模型常识)。"
        + "买家侧完全无症状,所以这个数是唯一的信号——线划得很低是刻意的" },
};

function toneOf(key: string, value: number): { dot: string; text: string } | null {
  const line = LINES[key];
  if (!line) return null;                    // 没有参考线的指标不着色(计数类没有好坏)
  const worse = line.lowerIsBetter
    ? (v: number, t: number) => v > t
    : (v: number, t: number) => v < t;
  if (worse(value, line.bad)) return { dot: "bg-destructive", text: "text-destructive" };
  if (worse(value, line.warn)) return { dot: "bg-amber-500", text: "text-amber-600 dark:text-amber-400" };
  return { dot: "bg-emerald-500", text: "" };
}

function lineLabel(key: string): string {
  const l = LINES[key];
  if (!l) return "";
  const fmt = (key.startsWith("latency") || key.startsWith("ttft")) ? ms : pct;
  return l.lowerIsBetter ? `参考 < ${fmt(l.warn)}` : `参考 > ${fmt(l.warn)}`;
}

type Item = { key: string; label: string; display: string; raw?: number };

/** 把窗口小时数说成人话。null/0/缺失 = 全部历史。 */
function windowLabel(hours?: number | null): string {
  if (hours === null || hours === undefined || hours <= 0) return "进程内全部 trace（累计）";
  if (hours < 24) return `近 ${hours} 小时`;
  const days = hours / 24;
  return Number.isInteger(days) ? `近 ${days} 天` : `近 ${hours} 小时`;
}

/** 分组:回答同一个问题的指标放在一起。
 *
 * 改造前 14 张卡片平铺,「估算成本」和「转人工率」并排——它们服务于完全不同的
 * 决策(要不要加预算 / AI 顶不顶得住),混在一列会让看板变成一张数字清单。
 */
const GROUPS: { title: string; hint: string; keys: string[] }[] = [
  { title: "服务质量", hint: "买家这一侧的体感",
    keys: ["total_traces", "ttft_p50", "ttft_p95", "ttft_holdback_cost",
           "latency_p50", "latency_p95", "error_rate"] },
  { title: "AI 能力", hint: "AI 自己顶住了多少", keys: ["tool_success_rate", "tool_calls", "handoffs", "escalation_rate"] },
  { title: "知识库", hint: "客服回答政策问题时有没有依据",
    keys: ["kb_degraded_rate", "kb_recall_degraded", "kb_recall_attempts"] },
  { title: "安全与合规", hint: "护栏拦下了什么", keys: ["guard_blocks", "block_rate", "guard_sanitizes"] },
  { title: "成本", hint: "这些对话花了多少", keys: ["est_cost", "total_prompt_tokens", "total_completion_tokens"] },
];

export function MetricCards({ m }: { m: Metrics }) {
  const all: Record<string, Item> = {
    total_traces: { key: "total_traces", label: "总请求数", display: num(m.total_traces) },
    error_rate: { key: "error_rate", label: "错误率", display: pct(m.error_rate), raw: m.error_rate },
    ttft_p50: { key: "ttft_p50", label: "首字 P50",
      display: m.streamed_traces ? ms(m.ttft_p50_ms ?? 0) : "—",
      raw: m.streamed_traces ? (m.ttft_p50_ms ?? 0) : undefined },
    ttft_p95: { key: "ttft_p95", label: "首字 P95",
      display: m.streamed_traces ? ms(m.ttft_p95_ms ?? 0) : "—" },
    // 分母用配对样本数,不是 streamed_traces:代价只有两条 span 都在的 trace 才算
    // 得出来(reply_visible 上线前落库的历史 trace 只有引擎侧那条)。
    ttft_holdback_cost: { key: "ttft_holdback_cost", label: "脱敏缓冲代价",
      display: m.ttft_holdback_paired ? ms(m.ttft_holdback_cost_p50_ms ?? 0) : "—",
      raw: m.ttft_holdback_paired ? (m.ttft_holdback_cost_p50_ms ?? 0) : undefined },
    latency_p50: { key: "latency_p50", label: "延迟 P50", display: ms(m.latency_p50_ms), raw: m.latency_p50_ms },
    latency_p95: { key: "latency_p95", label: "延迟 P95", display: ms(m.latency_p95_ms), raw: m.latency_p95_ms },
    tool_success_rate: { key: "tool_success_rate", label: "工具成功率", display: pct(m.tool_success_rate), raw: m.tool_success_rate },
    tool_calls: { key: "tool_calls", label: "工具调用数", display: num(m.tool_calls) },
    guard_blocks: { key: "guard_blocks", label: "护栏拦截", display: num(m.guard_blocks) },
    block_rate: { key: "block_rate", label: "拦截率", display: pct(m.block_rate) },
    guard_sanitizes: { key: "guard_sanitizes", label: "脱敏次数", display: num(m.guard_sanitizes) },
    handoffs: { key: "handoffs", label: "转人工数", display: num(m.handoffs) },
    escalation_rate: { key: "escalation_rate", label: "转人工率", display: pct(m.escalation_rate), raw: m.escalation_rate },
    kb_degraded_rate: { key: "kb_degraded_rate", label: "知识库降级率",
      display: m.kb_recall_attempts ? pct(m.kb_degraded_rate ?? 0) : "—",
      raw: m.kb_recall_attempts ? (m.kb_degraded_rate ?? 0) : undefined },
    kb_recall_degraded: { key: "kb_recall_degraded", label: "降级轮次",
      display: num(m.kb_recall_degraded ?? 0) },
    kb_recall_attempts: { key: "kb_recall_attempts", label: "尝试检索轮次",
      display: num(m.kb_recall_attempts ?? 0) },
    est_cost: { key: "est_cost", label: "估算成本", display: "$" + m.est_cost_usd.toFixed(4) },
    total_prompt_tokens: { key: "total_prompt_tokens", label: "Prompt tokens", display: num(m.total_prompt_tokens) },
    total_completion_tokens: { key: "total_completion_tokens", label: "Completion tokens", display: num(m.total_completion_tokens) },
  };

  return (
    <div className="flex flex-col gap-4">
      {GROUPS.map((g) => (
        <section key={g.title}>
          <div className="mb-1.5 flex items-baseline gap-2">
            <h3 className="text-sm font-semibold">{g.title}</h3>
            <span className="text-[11px] text-muted-foreground">{g.hint}</span>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {g.keys.map((k) => {
              const it = all[k];
              if (!it) return null;
              const tone = it.raw === undefined ? null : toneOf(k, it.raw);
              const line = LINES[k];
              return (
                <Card key={k} className="p-4" data-testid={`metric-${k}`}>
                  <div className="flex items-center gap-1.5">
                    {tone && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} />}
                    <span className="text-xs text-muted-foreground">{it.label}</span>
                  </div>
                  <div className={`mt-1 text-2xl font-semibold ${tone?.text || ""}`}>{it.display}</div>
                  {line && (
                    // 参考线与依据都写在卡上:一个不敢说出判据的阈值,和没有阈值
                    // 一样不可信。title 里放完整说明,避免卡片被撑高。
                    <div className="mt-1 text-[10px] text-muted-foreground" title={line.note}>
                      {lineLabel(k)}
                    </div>
                  )}
                </Card>
              );
            })}
          </div>
        </section>
      ))}
      <p className="text-[11px] text-muted-foreground">
        彩色圆点是<b>展示侧的判读参考</b>，不是后端策略、也不是对外承诺的 SLA；
        每条参考线的依据见卡片下方文字的悬停说明。
        统计口径：{windowLabel(m.window_hours)}
        {(m.window_hours ?? null) === null && (
          <span>（累计口径下，<b>已修好的问题也要很久才会从红色褪回来</b>——排查现状请切到较短窗口）</span>
        )}
      </p>
    </div>
  );
}
