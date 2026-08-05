import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { GrowthPanel } from "@/components/operations/GrowthPanel";

// 触达效果卡(N3):发送时记基线、到期按订单状态是否推进判定的转化率。
// 断言的重点不是数字本身,而是"口径必须钉在界面上"——归因窗口 N 小时、
// 只认状态向前推进——这条约束不写清楚,转化率就是一句可以随便解读的空话。

const STATS = {
  success: true, window_days: 30, window_hours: 24,
  sent: 10, converted: 4, conversion_rate: 0.4,
};

function stub(stats: unknown = STATS, drafts: unknown[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return { ok: true, json: async () => ({ success: true, sent: true, reason: "" }) };
    const u = String(url);
    if (u.includes("outreach-stats")) return { ok: true, json: async () => stats };
    if (u.includes("/opportunities")) return { ok: true, json: async () => ({ success: true, count: 0, opportunities: [] }) };
    return { ok: true, json: async () => ({ success: true, drafts }) };
  }));
}

describe("GrowthPanel 触达效果卡", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("展示已发送/已转化/转化率,数字来自后端", async () => {
    stub();
    render(<GrowthPanel />);
    const card = await screen.findByTestId("outreach-stats");
    expect(within(card).getByText("10")).toBeInTheDocument();   // 已发送
    expect(within(card).getByText("4")).toBeInTheDocument();    // 已转化
    expect(within(card).getByText("40.0%")).toBeInTheDocument(); // 转化率,与指标卡同款 pct() 格式
  });

  it("界面上必须写明测量窗口与归因窗口,不能只给一个裸的转化率数字", async () => {
    stub();
    render(<GrowthPanel />);
    const card = await screen.findByTestId("outreach-stats");
    // 统计窗口(近 N 天)与归因窗口(发出满 N 小时后才判定)都要能看到
    expect(within(card).getByText(/30 天/)).toBeInTheDocument();
    expect(within(card).getByText(/24 小时/)).toBeInTheDocument();
  });

  it("界面上必须写明'转化'的口径——只认状态向前推进,不含退款等其它变化", async () => {
    stub();
    render(<GrowthPanel />);
    const card = await screen.findByTestId("outreach-stats");
    expect(within(card).getByText(/向前推进/)).toBeInTheDocument();
    expect(within(card).getByText(/退款/)).toBeInTheDocument();
  });

  it("读取失败时不崩溃,只在这张卡片上报错,不连累草稿列表", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return { ok: true, json: async () => ({ success: true }) };
      const u = String(url);
      if (u.includes("outreach-stats")) return { ok: false, status: 500, json: async () => ({}) };
      if (u.includes("/opportunities")) return { ok: true, json: async () => ({ success: true, count: 0, opportunities: [] }) };
      return { ok: true, json: async () => ({ success: true, drafts: [] }) };
    }));
    render(<GrowthPanel />);
    expect(await screen.findByText(/触达效果读取失败/)).toBeInTheDocument();
    // 草稿列表这边仍然照常渲染空态,不被拖垮
    expect(await screen.findByText(/暂无待审草稿/)).toBeInTheDocument();
  });

  it("转化率为空数据(窗口内还没有发过消息)时不崩溃,显示 0.0%", async () => {
    stub({ success: true, window_days: 7, window_hours: 24, sent: 0, converted: 0, conversion_rate: 0.0 });
    render(<GrowthPanel />);
    const card = await screen.findByTestId("outreach-stats");
    expect(within(card).getByText("0.0%")).toBeInTheDocument();
  });
});
