import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { GrowthPanel } from "@/components/operations/GrowthPanel";

// 跟进链小节(N7:持续沟通=序列自动推进,不是自动发送)。断言重点:
// - 每条链显示买家/类型/第 N 步/共 M 步/状态;
// - 已终止的链常驻显示中文终止原因,不能悄悄消失——不然店主看不出为什么
//   不再跟了;
// - 这张卡片自己的 busy/error 状态独立,读取失败不连累其它三张既有卡片
//   (触达效果/商机概览/待审草稿),反过来也一样。

const ACTIVE_FOLLOWUP = {
  id: 1, user_id: "u1", kind: "unpaid_order", kind_label: "下单未支付",
  correlation_id: "C1", step: 2, max_steps: 3,
  next_touch_at: "2026-08-09 10:00:00", status: "active", stop_reason: null,
  created_at: "2026-08-05 10:00:00", updated_at: "2026-08-07 10:00:00",
};

const STOPPED_FOLLOWUP = {
  id: 2, user_id: "u2", kind: "abandoned_cart", kind_label: "加购未下单",
  correlation_id: "C2", step: 1, max_steps: 3,
  next_touch_at: "2026-08-06 10:00:00", status: "stopped",
  stop_reason: "in_service",
  stop_reason_label: "该买家正由人工处理中(接管中/有未结工单),已停止自动跟进",
  created_at: "2026-08-05 10:00:00", updated_at: "2026-08-06 10:00:00",
};

function stub(followups: unknown[] = [], drafts: unknown[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return { ok: true, json: async () => ({ success: true, sent: true, reason: "" }) };
    const u = String(url);
    if (u.includes("/followups")) return { ok: true, json: async () => ({ success: true, followups }) };
    if (u.includes("outreach-stats")) {
      return { ok: true, json: async () => ({ success: true, window_days: 30, window_hours: 24, sent: 0, converted: 0, conversion_rate: 0 }) };
    }
    if (u.includes("opportunity-kinds")) return { ok: true, json: async () => ({ success: true, kinds: [] }) };
    if (u.includes("/opportunities")) return { ok: true, json: async () => ({ success: true, count: 0, opportunities: [] }) };
    return { ok: true, json: async () => ({ success: true, drafts }) };
  }));
}

describe("GrowthPanel 跟进链小节", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("展示进行中的链:买家、类型、第 N/共 M 步、下次触达时间", async () => {
    stub([ACTIVE_FOLLOWUP]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("followup-1");
    expect(within(card).getByText(/u1/)).toBeInTheDocument();
    expect(within(card).getByText("下单未支付")).toBeInTheDocument();
    expect(within(card).getByText(/第 2 \/ 共 3 步/)).toBeInTheDocument();
    expect(within(card).getByText("进行中")).toBeInTheDocument();
    expect(within(card).getByText(/2026-08-09 10:00:00/)).toBeInTheDocument();
  });

  it("已终止的链常驻显示中文终止原因,不会从列表里消失", async () => {
    stub([STOPPED_FOLLOWUP]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("followup-2");
    expect(within(card).getByText("已终止")).toBeInTheDocument();
    expect(within(card).getByText(/人工处理中/)).toBeInTheDocument();
  });

  it("无跟进链时显示空态,不影响其它卡片正常渲染", async () => {
    stub([]);
    render(<GrowthPanel />);
    expect(await screen.findByText("暂无跟进链")).toBeInTheDocument();
    expect(await screen.findByText(/暂无待审草稿/)).toBeInTheDocument();
  });

  it("跟进链读取失败时只在这张卡片报错,不连累草稿列表/商机概览/触达效果", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return { ok: true, json: async () => ({ success: true }) };
      const u = String(url);
      if (u.includes("/followups")) return { ok: false, status: 500, json: async () => ({}) };
      if (u.includes("outreach-stats")) {
        return { ok: true, json: async () => ({ success: true, window_days: 30, window_hours: 24, sent: 0, converted: 0, conversion_rate: 0 }) };
      }
      if (u.includes("opportunity-kinds")) return { ok: true, json: async () => ({ success: true, kinds: [] }) };
      if (u.includes("/opportunities")) return { ok: true, json: async () => ({ success: true, count: 0, opportunities: [] }) };
      return { ok: true, json: async () => ({ success: true, drafts: [] }) };
    }));
    render(<GrowthPanel />);
    expect(await screen.findByText(/跟进链读取失败/)).toBeInTheDocument();
    // 其它卡片仍正常渲染(触达效果卡与空的待审草稿列表)
    expect(await screen.findByTestId("outreach-stats")).toBeInTheDocument();
    expect(await screen.findByText(/暂无待审草稿/)).toBeInTheDocument();
  });

  it("其它卡片读取失败时不连累跟进链小节的正常展示", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return { ok: true, json: async () => ({ success: true }) };
      const u = String(url);
      if (u.includes("/followups")) return { ok: true, json: async () => ({ success: true, followups: [ACTIVE_FOLLOWUP] }) };
      if (u.includes("outreach-stats")) return { ok: false, status: 500, json: async () => ({}) };
      if (u.includes("opportunity-kinds")) return { ok: true, json: async () => ({ success: true, kinds: [] }) };
      if (u.includes("/opportunities")) return { ok: true, json: async () => ({ success: true, count: 0, opportunities: [] }) };
      return { ok: true, json: async () => ({ success: true, drafts: [] }) };
    }));
    render(<GrowthPanel />);
    expect(await screen.findByTestId("followup-1")).toBeInTheDocument();
    expect(await screen.findByText(/触达效果读取失败/)).toBeInTheDocument();
  });
});
