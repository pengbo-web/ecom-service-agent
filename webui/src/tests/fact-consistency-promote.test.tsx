/** 事实一致性拦下转正时,界面要把**丢了哪几条**摆出来,由人判断。
 *
 * 关键点在于这次征求的是与 force 不同的另一个知情同意:force 是"我知道没跑门禁",
 * 这里是"我知道我在删掉这几条硬事实"。合成一个开关的话,界面上永远带 force 的
 * 那个按钮会让这道闸从来不生效——一道只在 CLI 上有效的闸不叫闸。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { SkillsView } from "@/components/SkillsView";

const OVERVIEW = {
  live: [], candidates: [{
    name: "track-order", path: "p", valid: true, unknown_tools: [], errors: [],
    is_improvement: true, risk: "low", policy: "canary_ab",
    gate_cases: 3, gate_evaluable: true, gate_underpowered: false, gate_note: "",
  }], traces: {}, traces_window: { limit: 200, note: "" }, canaries: [],
};

const BLOCKED = {
  success: true, promoted: false, reason: "事实一致性未通过",
  fact_check: {
    ok: false, applicable: true, reason: "丢失 7天",
    baseline_source: "归档最早快照 20260804-232032",
    lost_since_baseline: ["7天", "`query_logistics`"],
    lost_since_previous: ["7天"],
    lost_before_this_round: ["`query_logistics`"],
    added: [], bloat_ratio: 0.1,
  },
};

let urls: string[] = [];

function stub(responses: any[]) {
  let i = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: any) => {
    if (init?.method === "POST") {
      urls.push(String(url));
      return { ok: true, json: async () => responses[i++] };
    }
    return { ok: true, json: async () => OVERVIEW };
  }));
}

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); urls = []; });

describe("事实一致性拦截后的二次确认", () => {
  it("列出丢失的每一条,并区分本轮删的与更早丢的", async () => {
    stub([BLOCKED]);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    stub([BLOCKED, { success: true, promoted: true, risk: "low" }]);
    render(<SkillsView />);
    fireEvent.click(await screen.findByTestId("promote-track-order"));

    await waitFor(() => expect(confirm.mock.calls.length).toBeGreaterThan(1));
    const msg = confirm.mock.calls[confirm.mock.calls.length - 1][0] as string;
    expect(msg).toMatch(/7天/);
    expect(msg).toMatch(/本轮删除：7天/);
    expect(msg).toMatch(/更早的轮次已丢失/);
    expect(msg).toMatch(/归档最早快照 20260804-232032/);
  });

  it("确认后才带 allow_fact_loss 重试", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    stub([BLOCKED, { success: true, promoted: true, risk: "low" }]);
    render(<SkillsView />);
    fireEvent.click(await screen.findByTestId("promote-track-order"));

    await waitFor(() => expect(urls.length).toBe(2));
    expect(urls[0]).not.toMatch(/allow_fact_loss/);   // 第一次绝不自带放行
    expect(urls[1]).toMatch(/allow_fact_loss=true/);
  });

  it("拒绝时不上线,也不再发第二次请求", async () => {
    const confirm = vi.spyOn(window, "confirm");
    confirm.mockReturnValueOnce(true).mockReturnValueOnce(false);
    stub([BLOCKED]);
    render(<SkillsView />);
    fireEvent.click(await screen.findByTestId("promote-track-order"));

    expect(await screen.findByTestId("act-ok-track-order")).toHaveTextContent("已取消");
    expect(urls.length).toBe(1);
  });

  it("事实一致性通过时不多问一句", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    stub([{ success: true, promoted: true, risk: "low",
            fact_check: { ok: true, applicable: true, reason: "通过",
                          lost_since_baseline: [], lost_since_previous: [],
                          lost_before_this_round: [], added: [], bloat_ratio: 0 } }]);
    render(<SkillsView />);
    fireEvent.click(await screen.findByTestId("promote-track-order"));

    await waitFor(() => expect(urls.length).toBe(1));
    expect(confirm.mock.calls.length).toBe(1);   // 只有原来那次"未经门禁"确认
  });
});
