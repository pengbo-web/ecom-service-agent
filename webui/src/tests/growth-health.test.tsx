import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { GrowthPanel } from "@/components/operations/GrowthPanel";

// 协作健康卡:失败事件 + worker 心跳。
// 这张卡存在的理由是补一个可见性缺口——系统刻意不自动重试失败事件,那它就
// 必须在界面上有位置,否则"留在表里供人工决定"等于留给没人。

const HEALTHY = {
  success: true, failed_count: 0, failed: [],
  worker: {
    name: "collab", last_success_at: "2026-08-08 12:00:00", last_error_at: null,
    last_error: null, stale_seconds: 30, threshold_seconds: 360, healthy: true,
  },
};

function stub(health: unknown, onPost?: (url: string) => void) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (init?.method === "POST") {
      onPost?.(u);
      return { ok: true, json: async () => ({ success: true, changed: true, message: "已放回队列" }) };
    }
    if (u.includes("collab/health")) return { ok: true, json: async () => health };
    if (u.includes("outreach-stats")) {
      return { ok: true, json: async () => ({ success: true, window_days: 30, window_hours: 24, sent: 0, converted: 0, conversion_rate: 0 }) };
    }
    if (u.includes("opportunity-kinds")) return { ok: true, json: async () => ({ success: true, kinds: [] }) };
    if (u.includes("/opportunities")) return { ok: true, json: async () => ({ success: true, count: 0, opportunities: [] }) };
    if (u.includes("followups")) return { ok: true, json: async () => ({ success: true, followups: [] }) };
    return { ok: true, json: async () => ({ success: true, drafts: [] }) };
  }));
}

describe("GrowthPanel 协作健康卡", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("worker 正常时只给一行浅色提示,不抢注意力", async () => {
    stub(HEALTHY);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("collab-health");
    expect(within(card).getByText(/协作 worker 正常/)).toBeInTheDocument();
  });

  it("worker 停摆时明确说出上次跑完的时间与影响范围", async () => {
    stub({ ...HEALTHY, worker: { ...HEALTHY.worker, healthy: false, stale_seconds: 9999 } });
    render(<GrowthPanel />);
    const card = await screen.findByTestId("collab-health");
    expect(within(card).getByText(/已停摆/)).toBeInTheDocument();
    expect(within(card).getByText(/2026-08-08 12:00:00/)).toBeInTheDocument();
    // 必须说清"买家链路不受影响",否则运营会误判成线上事故
    expect(within(card).getByText(/买家链路不受影响/)).toBeInTheDocument();
  });

  it("从未运行过与停摆是两种提示:前者给出启动命令", async () => {
    stub({ ...HEALTHY, worker: { ...HEALTHY.worker, healthy: false, last_success_at: null, stale_seconds: null } });
    render(<GrowthPanel />);
    const card = await screen.findByTestId("collab-health");
    expect(within(card).getByText(/从未运行过/)).toBeInTheDocument();
    expect(within(card).getByText(/agent_collab --loop/)).toBeInTheDocument();
  });

  it("有失败事件时列出来,并说明系统不会自动重试", async () => {
    stub({
      ...HEALTHY, failed_count: 1,
      failed: [{ id: 7, event_type: "signal.anomaly", source_agent: "service",
                 target_agent: "analyst", correlation_id: "C7",
                 created_at: "2026-08-08 11:00:00", consumed_at: null }],
    });
    render(<GrowthPanel />);
    const card = await screen.findByTestId("collab-health");
    expect(within(card).getByText(/不会自动重试/)).toBeInTheDocument();
    expect(within(card).getByTestId("failed-event-7")).toBeInTheDocument();
    expect(within(card).getByText(/C7/)).toBeInTheDocument();
  });

  it("重试按钮打到正确的端点", async () => {
    const posted: string[] = [];
    stub({
      ...HEALTHY, failed_count: 1,
      failed: [{ id: 7, event_type: "signal.anomaly", source_agent: "service",
                 target_agent: "analyst", correlation_id: "C7",
                 created_at: "2026-08-08 11:00:00", consumed_at: null }],
    }, (u) => posted.push(u));
    render(<GrowthPanel />);
    const row = await screen.findByTestId("failed-event-7");
    fireEvent.click(within(row).getByRole("button", { name: /放回队列重试/ }));
    await waitFor(() =>
      expect(posted.some((u) => u.includes("/api/admin/collab/failed/7/retry"))).toBe(true));
  });

  it("响应缺字段时只丢这张卡,不能把整个面板带崩", async () => {
    // 旧版服务端 / 代理返回的 200 错误页 / 灰度期新旧端点并存,都会长这样。
    // 一个健康卡片绝不该有能力搞垮它所监控的页面——那页上就是人工审批闸。
    stub({ success: true });
    render(<GrowthPanel />);
    const card = await screen.findByTestId("collab-health");
    expect(within(card).getByText(/响应格式不正确/)).toBeInTheDocument();
    // 同一页上的其它卡片照常渲染
    expect(await screen.findByTestId("outreach-stats")).toBeInTheDocument();
  });
});
