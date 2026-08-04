import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { OperationsView } from "@/components/OperationsView";

const DATA = {
  overview: { success: true, window_days: 7, orders: 12, gmv: 4788, avg_order_value: 399,
              refunds: 3, refund_rate: 0.25, cancels: 0, cancel_rate: 0,
              conversations: 30, orders_per_conversation: 0.4 },
  products: { success: true, window_days: 7, products: [] },
  anomalies: [{ kind: "refund_rate_high", subject: "P001", subject_name: "跑鞋",
                value: 0.25, threshold: 0.15, detail: { orders: 12, refunds: 3,
                top_reason: "尺码不准" } }],
};

describe("OperationsView", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => DATA })));
    localStorage.clear();
  });

  it("渲染关键指标", async () => {
    render(<OperationsView />);
    expect(await screen.findByText(/25(\.0)?%/)).toBeInTheDocument();   // 退款率
    expect(await screen.findByText("12")).toBeInTheDocument();          // 订单量
  });

  it("异常显示当前值与告警线", async () => {
    render(<OperationsView />);
    expect(await screen.findByText(/跑鞋/)).toBeInTheDocument();
    expect(await screen.findByText(/告警线/)).toBeInTheDocument();
  });

  it("标出统计窗口,数字不带窗口对店主没意义", async () => {
    render(<OperationsView />);
    expect(await screen.findByText(/近\s*7\s*天/)).toBeInTheDocument();
  });

  it("无异常时给明确空态而不是留白", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, json: async () => ({ ...DATA, anomalies: [] }) })));
    render(<OperationsView />);
    expect(await screen.findByText(/无跨线异常/)).toBeInTheDocument();
  });

  it("加载失败时旧数据要标过期,不能装作是新数据", async () => {
    const f = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => DATA })
      .mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) });
    vi.stubGlobal("fetch", f);
    const { rerender } = render(<OperationsView />);
    await screen.findByText("12");
    rerender(<OperationsView key="2" />);
    // 断言:错误横幅出现后,页面上要能找到"数据可能已过期"的标记
    expect(await screen.findByText(/已过期|过期/)).toBeInTheDocument();
  });
});
