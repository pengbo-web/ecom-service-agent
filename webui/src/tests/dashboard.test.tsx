import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricCards } from "@/components/MetricCards";

const M = {
  total_traces: 12, error_rate: 0.0, latency_p50_ms: 800, latency_p95_ms: 1500,
  tool_success_rate: 1.0, tool_calls: 5, guard_blocks: 1, block_rate: 0.08,
  guard_sanitizes: 2, handoffs: 0, escalation_rate: 0.0,
  total_prompt_tokens: 100, total_completion_tokens: 50, est_cost_usd: 0.01,
  intent_distribution: { order_query: 3, greeting: 2 },
};

describe("MetricCards", () => {
  it("渲染核心指标", () => {
    render(<MetricCards m={M as any} />);
    expect(screen.getByText("总请求数")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText(/P95/)).toBeInTheDocument();
  });
});
