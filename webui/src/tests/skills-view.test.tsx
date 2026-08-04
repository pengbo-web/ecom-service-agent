import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SkillsView } from "@/components/SkillsView";

const OVERVIEW = {
  live: [{ name: "process-return", description: "退货退款处理。" }],
  candidates: [
    { name: "track-order", path: "p1", valid: true, unknown_tools: [], errors: [],
      is_improvement: true, risk: "low", policy: "canary_ab" },
    { name: "money-one", path: "p2", valid: true, unknown_tools: [], errors: [],
      is_improvement: false, risk: "high", policy: "manual" },
    { name: "unknown-one", path: "p3", valid: false, unknown_tools: ["order_list"],
      errors: ["引用了未知工具: order_list"], is_improvement: false, risk: null, policy: null },
  ],
  traces: { "track-order": { success: 3, tool_error: 11 } },
  traces_window: { limit: 500, note: "按最近轨迹计数的窗口值,非全时段统计" },
  canaries: [{ skill_name: "track-order", candidate_path: "p1", percent: 50, risk: "low",
               policy: "canary_ab", status: "active", started_at: "2026-08-04 23:13:07",
               finished_at: null }],
};

describe("SkillsView", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => OVERVIEW })));
    localStorage.clear();
  });

  it("渲染现行技能与候选", async () => {
    render(<SkillsView />);
    expect(await screen.findByText("process-return")).toBeInTheDocument();
    // track-order 同时出现在"待审候选"与"活跃灰度"两段(候选正在灰度中,真实状态如此),
    // 用 findAllByText 而非 findByText,避免因多处命中而误判为渲染失败
    expect((await screen.findAllByText("track-order")).length).toBeGreaterThan(0);
  });

  it("高危候选标出需人工", async () => {
    render(<SkillsView />);
    expect(await screen.findByText(/需人工确认/)).toBeInTheDocument();
  });

  it("风险判不了的候选渲染成需人工复核,不能显示成低危", async () => {
    render(<SkillsView />);
    expect(await screen.findByText(/未判定/)).toBeInTheDocument();
  });

  it("展示灰度分流与取样窗口说明", async () => {
    render(<SkillsView />);
    expect(await screen.findByText(/50%/)).toBeInTheDocument();
    expect(await screen.findByText(/非全时段/)).toBeInTheDocument();
  });
});
