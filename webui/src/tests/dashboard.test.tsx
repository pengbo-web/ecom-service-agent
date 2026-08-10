import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MetricCards } from "@/components/MetricCards";

// 改造前 14 张卡片颜色完全一致:`延迟 P50 = 11516 ms` 和 `总请求数 = 473`
// 在视觉上没有任何区别。而 11.5 秒的中位延迟对在线客服是灾难级的——运营盯着
// 看板也看不出这里出了事。指标没有参考线就只是数字。

const HEALTHY = {
  total_traces: 12, error_rate: 0.0, latency_p50_ms: 800, latency_p95_ms: 1500,
  tool_success_rate: 1.0, tool_calls: 5, guard_blocks: 1, block_rate: 0.08,
  guard_sanitizes: 2, handoffs: 0, escalation_rate: 0.0,
  total_prompt_tokens: 100, total_completion_tokens: 50, est_cost_usd: 0.01,
  intent_distribution: { order_query: 3, greeting: 2 },
};

// 实跑时看板上的真实数字。
const REAL_BAD = { ...HEALTHY, latency_p50_ms: 11516, latency_p95_ms: 40021 };

describe("MetricCards", () => {
  it("渲染核心指标", () => {
    render(<MetricCards m={HEALTHY as any} />);
    expect(screen.getByText("总请求数")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText(/P95/)).toBeInTheDocument();
  });

  it("按角色分组,不是 14 张卡平铺", () => {
    // 「估算成本」和「转人工率」服务于完全不同的决策(要不要加预算 /
    // AI 顶不顶得住),混在一列会让看板退化成一张数字清单。
    render(<MetricCards m={HEALTHY as any} />);
    for (const g of ["服务质量", "AI 能力", "安全与合规", "成本"]) {
      expect(screen.getByText(g)).toBeInTheDocument();
    }
  });

  it("越线的指标变红,健康的不变", () => {
    render(<MetricCards m={REAL_BAD as any} />);
    const p50 = screen.getByTestId("metric-latency_p50");
    expect(within(p50).getByText("11.5 s").className).toContain("text-destructive");

    const traces = screen.getByTestId("metric-total_traces");
    expect(within(traces).getByText("12").className).not.toContain("text-destructive");
  });

  it("秒级延迟用秒显示,不是五位数毫秒", () => {
    // 「11516 ms」需要读的人自己心算;「11.5 s」一眼就知道有多糟。
    render(<MetricCards m={REAL_BAD as any} />);
    expect(screen.getByText("11.5 s")).toBeInTheDocument();
    expect(screen.queryByText("11516 ms")).toBeNull();
  });

  it("计数类指标不着色(没有好坏之分)", () => {
    const { container } = render(<MetricCards m={HEALTHY as any} />);
    const calls = screen.getByTestId("metric-tool_calls");
    expect(calls.querySelector(".rounded-full")).toBeNull();
    expect(container).toBeTruthy();
  });

  it("每条参考线都写出判据,并声明不是 SLA", () => {
    // 一个不敢说出判据的阈值,和没有阈值一样不可信;而把展示侧的判读参考
    // 说成"指标合规",是拿一张前端常量表冒充对外承诺。
    render(<MetricCards m={HEALTHY as any} />);
    const p50 = screen.getByTestId("metric-latency_p50");
    expect(within(p50).getByText(/参考 </).getAttribute("title")).toMatch(/3 秒/);
    expect(screen.getByText(/不是后端策略、也不是对外承诺的 SLA/)).toBeInTheDocument();
  });

  it("方向相反的指标判读也要正确(成功率是越高越好)", () => {
    render(<MetricCards m={{ ...HEALTHY, tool_success_rate: 0.5 } as any} />);
    const el = screen.getByTestId("metric-tool_success_rate");
    expect(within(el).getByText("50.0%").className).toContain("text-destructive");
  });
});
