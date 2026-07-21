import { Card } from "@/components/ui/card";

export type Metrics = {
  total_traces: number; error_rate: number; latency_p50_ms: number; latency_p95_ms: number;
  tool_success_rate: number; tool_calls: number; guard_blocks: number; block_rate: number;
  guard_sanitizes: number; handoffs: number; escalation_rate: number;
  total_prompt_tokens: number; total_completion_tokens: number; est_cost_usd: number;
  intent_distribution: Record<string, number>;
};
const pct = (x: number) => (x * 100).toFixed(1) + "%";
const ms = (x: number) => x.toFixed(0) + " ms";

export function MetricCards({ m }: { m: Metrics }) {
  const items: [string, string | number][] = [
    ["总请求数", m.total_traces], ["错误率", pct(m.error_rate)],
    ["延迟 P50", ms(m.latency_p50_ms)], ["延迟 P95", ms(m.latency_p95_ms)],
    ["工具成功率", pct(m.tool_success_rate)], ["工具调用数", m.tool_calls],
    ["护栏拦截", m.guard_blocks], ["拦截率", pct(m.block_rate)],
    ["脱敏次数", m.guard_sanitizes], ["转人工数", m.handoffs],
    ["转人工率", pct(m.escalation_rate)], ["估算成本", "$" + m.est_cost_usd.toFixed(4)],
    ["Prompt tokens", m.total_prompt_tokens], ["Completion tokens", m.total_completion_tokens],
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      {items.map(([label, value]) => (
        <Card key={label} className="p-4">
          <div className="text-xs text-muted-foreground">{label}</div>
          <div className="mt-1 text-2xl font-semibold">{value}</div>
        </Card>
      ))}
    </div>
  );
}
