/** 成功率必须带口径:这个数字是真实买家打出来的，还是压测打出来的。
 *
 * 实测 track-order 全量 53 轮算出 28%，只算真实流量 17 轮是 88% —— 那 28% 压根
 * 不是关于这份 skill 的陈述。而看门狗拿同一批数据做自动回滚判定。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { SkillsView } from "@/components/SkillsView";

function stub(over: any = {}) {
  const payload = {
    live: [{ name: "track-order", description: "查物流" }],
    candidates: [], traces: { "track-order": { success: 15, tool_error: 38 } },
    traces_window: { limit: 0, note: "全时段统计" }, canaries: [], ...over,
  };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => payload })));
}

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); });

describe("成功率的流量口径", () => {
  it("标出其中有多少是真实流量", async () => {
    stub({ traces_live: { "track-order": { success: 15, tool_error: 2 } } });
    render(<SkillsView />);
    expect((await screen.findByTestId("traffic-scope-track-order")).textContent)
      .toMatch(/真实流量 17 轮/);
  });

  it("真实流量为 0 时明说这个数字不能当线上指标读", async () => {
    stub({ traces_live: {} });
    render(<SkillsView />);
    const el = await screen.findByTestId("traffic-scope-track-order");
    expect(el.textContent).toMatch(/真实流量 0 轮/);
    expect(el.textContent).toMatch(/不能当线上指标读/);
  });

  it("真实流量占比过半时不加警示色", async () => {
    stub({ traces: { "track-order": { success: 50 } },
           traces_live: { "track-order": { success: 45 } } });
    render(<SkillsView />);
    const el = await screen.findByTestId("traffic-scope-track-order");
    expect(el.className).not.toMatch(/amber/);
  });

  it("占比不足一半时加警示色", async () => {
    stub({ traces_live: { "track-order": { success: 5 } } });
    render(<SkillsView />);
    expect((await screen.findByTestId("traffic-scope-track-order")).className)
      .toMatch(/amber/);
  });

  it("老后端没给 traces_live 时不显示,也不猜", async () => {
    stub();
    render(<SkillsView />);
    await screen.findByText("track-order");
    expect(screen.queryByTestId("traffic-scope-track-order")).toBeNull();
  });
});

describe("全库来源构成", () => {
  it("按条数降序列出每一类", async () => {
    stub({ trace_sources: { dev: 858, live: 62, loadtest: 41, eval: 15 } });
    render(<SkillsView />);
    const el = await screen.findByTestId("trace-sources");
    expect(el.textContent).toMatch(/走查 858/);
    expect(el.textContent).toMatch(/真实 62/);
    expect(el.textContent).toMatch(/压测 41/);
    // 降序:走查排在真实之前
    expect(el.textContent!.indexOf("走查")).toBeLessThan(el.textContent!.indexOf("真实"));
  });

  it("一条真实流量都没有时说明看门狗因此不会动作", async () => {
    stub({ trace_sources: { unknown: 976 } });
    render(<SkillsView />);
    expect((await screen.findByTestId("trace-sources")).textContent)
      .toMatch(/看门狗的自动转正\/回滚因此不会动作/);
  });

  it("字段缺失时整行不显示", async () => {
    stub();
    render(<SkillsView />);
    await screen.findByText("track-order");
    expect(screen.queryByTestId("trace-sources")).toBeNull();
  });
});
