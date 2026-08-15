/** 失败归因面板:光看成功率会把"安全机制在工作"读成"这个 skill 很烂"。
 *
 * 实测:track-order 的 success 15 / tool_error 38,而 41 条失败里 37 条是**归属
 * 校验正确地拦住了跨用户访问**(订单属于「小明」,发起的是压测用户)。少了这一行,
 * 运营看着 28% 的成功率会去改一份一个字都没写错的流程文档。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { SkillsView } from "@/components/SkillsView";

function stub(overview: any) {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => overview })));
}

function payload(failure_attribution?: any) {
  return {
    live: [{ name: "track-order", description: "查物流" }],
    candidates: [], traces: { "track-order": { success: 15, tool_error: 38 } },
    traces_window: { limit: 200, note: "" }, canaries: [],
    ...(failure_attribution === undefined ? {} : { failure_attribution }),
  };
}

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); });

describe("失败归因", () => {
  it("把不该算在 skill 头上的失败单独标出来", async () => {
    stub(payload({ "track-order": { capability_limit: 37, evaluation_noise: 1 } }));
    render(<SkillsView />);
    const el = await screen.findByTestId("failure-attr-track-order");
    expect(el.textContent).toMatch(/38 次失败归因/);
    expect(el.textContent).toMatch(/权限\/依赖边界 37/);
  });

  it("一条都不指向 skill 时说清楚「改流程文档解决不了」", async () => {
    stub(payload({ "track-order": { capability_limit: 37, evaluation_noise: 1 } }));
    render(<SkillsView />);
    const el = await screen.findByTestId("failure-attr-track-order");
    expect(el.textContent).toMatch(/改流程文档解决不了/);
  });

  it("真有知识缺口时不打那句宽慰话", async () => {
    stub(payload({ "track-order": { knowledge_gap: 3, capability_limit: 1 } }));
    render(<SkillsView />);
    const el = await screen.findByTestId("failure-attr-track-order");
    expect(el.textContent).toMatch(/缺知识 3/);
    expect(el.textContent).not.toMatch(/改流程文档解决不了/);
  });

  it("「判不出」照常显示,不因为不好看就藏起来", async () => {
    stub(payload({ "track-order": { undetermined: 5 } }));
    render(<SkillsView />);
    expect((await screen.findByTestId("failure-attr-track-order")).textContent)
      .toMatch(/判不出 5/);
  });

  it("老后端没有这个字段时不显示,也不崩", async () => {
    stub(payload(undefined));
    render(<SkillsView />);
    await screen.findByText("track-order");
    expect(screen.queryByTestId("failure-attr-track-order")).toBeNull();
  });

  it("这个 skill 一次失败都没有时不占版面", async () => {
    stub(payload({ "other-skill": { knowledge_gap: 1 } }));
    render(<SkillsView />);
    await screen.findByText("track-order");
    expect(screen.queryByTestId("failure-attr-track-order")).toBeNull();
  });
});
