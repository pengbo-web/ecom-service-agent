import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { GrowthPanel } from "@/components/operations/GrowthPanel";

const DRAFT = {
  id: 1, opportunity_type: "stale_pending_order", user_id: "u1", order_id: "O1",
  content: "这款鞋我们更新了尺码建议,可以参考下", offer: {}, reason: "未付款",
  correlation_id: "C1", status: "draft", needs_review_reason: "",
  created_by: "growth", reviewed_by: null, created_at: "2026-08-05 10:00:00",
};

const FLAGGED = { ...DRAFT, id: 2, content: "全额退运费", needs_review_reason: "包含金钱承诺词: 退运费" };

function stub(drafts: unknown[], approveBody: unknown = { success: true, sent: true }) {
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") return { ok: true, json: async () => approveBody };
    return { ok: true, json: async () => ({ success: true, drafts }) };
  }));
}

describe("GrowthPanel", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("渲染草稿正文与目标买家", async () => {
    stub([DRAFT]);
    render(<GrowthPanel />);
    expect(await screen.findByText(/尺码建议/)).toBeInTheDocument();
    expect(await screen.findByText(/u1/)).toBeInTheDocument();
  });

  it("命中承诺词的草稿必须标出原因", async () => {
    stub([FLAGGED]);
    render(<GrowthPanel />);
    expect(await screen.findByText(/金钱承诺词/)).toBeInTheDocument();
  });

  it("批准前必须二次确认——发给买家是不可逆的", async () => {
    stub([DRAFT]);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /批准/ }));
    expect(confirmSpy).toHaveBeenCalled();
    // 用户点了取消 → 不能发出任何 POST
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.some((c) => (c[1] as RequestInit | undefined)?.method === "POST")).toBe(false);
  });

  it("确认后才真的调批准接口", async () => {
    stub([DRAFT]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /批准/ }));
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls.some((c) => String(c[0]).includes("/approve"))).toBe(true);
    });
  });

  it("发送失败要把原因显示出来", async () => {
    stub([DRAFT], { success: false, sent: false, reason: "投递失败,已退回待审,可重试" });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /批准/ }));
    expect(await screen.findByText(/已退回待审/)).toBeInTheDocument();
  });

  it("空态给明确文案", async () => {
    stub([]);
    render(<GrowthPanel />);
    expect(await screen.findByText(/暂无待审草稿/)).toBeInTheDocument();
  });
});
