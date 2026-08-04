import { describe, it, expect, vi, beforeEach } from "vitest";
import { getSkillsOverview } from "@/lib/api";

const OVERVIEW = {
  live: [{ name: "process-return", description: "退货退款处理。适用关键词：退货、退款。" }],
  candidates: [
    { name: "track-order", path: "p1", valid: true, unknown_tools: [], errors: [],
      is_improvement: true, risk: "low", policy: "canary_ab" },
    { name: "broken-one", path: "p2", valid: false, unknown_tools: ["order_list"],
      errors: ["引用了未知工具: order_list"], is_improvement: false, risk: null, policy: null },
  ],
  traces: { "track-order": { success: 3, tool_error: 11 } },
  traces_window: { limit: 500, note: "按最近轨迹计数的窗口值,非全时段统计" },
  canaries: [{ skill_name: "track-order", candidate_path: "p1", percent: 50, risk: "low",
               policy: "canary_ab", status: "active", started_at: "2026-08-04 23:13:07",
               finished_at: null }],
};

describe("skills overview api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => OVERVIEW })));
    localStorage.clear();
  });

  it("解析总览四段", async () => {
    const d = await getSkillsOverview();
    expect(d.live[0].name).toBe("process-return");
    expect(d.candidates).toHaveLength(2);
    expect(d.candidates[0].policy).toBe("canary_ab");
    expect(d.traces["track-order"].tool_error).toBe(11);
    expect(d.traces_window.limit).toBe(500);
    expect(d.canaries[0].percent).toBe(50);
  });

  it("判不了风险的候选保留 null(不得被当成低危)", async () => {
    const d = await getSkillsOverview();
    expect(d.candidates[1].risk).toBeNull();
    expect(d.candidates[1].policy).toBeNull();
  });
});
