/**
 * 候选卡片必须显示"门禁评不评得了"——在人点转正之前。
 *
 * 实测(走查自进化闭环时):/api/admin/skills 返回 7 个候选,其中 6 个的
 * gate_cases=0。它们不是在"排队等看门狗处理",而是每一轮 --start-all 都会打同一行
 * gate_unavailable、永远如此:新建 skill 走 gate_then_watch,那条路要求先过离线门禁,
 * 而没有任何机制会为新 skill 产门禁用例(蒸馏不产、上传不带、界面无入口)。
 *
 * 运维看着这份待审列表会以为自动化在推进。这个误解只有在页面上说出来才有出口。
 *
 * 同时锁住那个区分:"门禁评不了" ≠ "候选不达标"。invoice-issuance 实测校验全过、
 * 引用的工具真实存在——文案不能让人去改一个没有问题的候选。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SkillsView } from "@/components/SkillsView";

/** 与 skills-view.test.tsx 同一个惯例:stub 全局 fetch,不 mock api 模块。
 *
 * 转正走 POST,总览走 GET;这里按 method 分流,并把每次 POST 的 URL 记下来——
 * "界面这条路一直传 force=true"这条断言要看的就是 URL 上的 query。
 */
function stubFetch(overview: any, posts: string[]) {
  vi.stubGlobal("fetch", vi.fn(async (url: any, init?: any) => {
    const u = String(url);
    if ((init?.method || "GET").toUpperCase() === "POST") {
      posts.push(u);
      return { ok: true, json: async () => ({ promoted: true, risk: "medium", backup: null }) };
    }
    return { ok: true, json: async () => overview };
  }));
}

function candidate(over: Partial<any> = {}) {
  return {
    name: "invoice-issuance", path: "app/agent/skills/definitions/_candidates/invoice-issuance/SKILL.md",
    valid: true, unknown_tools: [], errors: [], is_improvement: false,
    risk: "medium", policy: "gate_then_watch",
    gate_cases: 0, gate_evaluable: false, gate_underpowered: false,
    gate_note: "评测集里没有任何用例点名覆盖 invoice-issuance",
    ...over,
  };
}

function payload(cands: any[]) {
  return {
    live: [], candidates: cands, traces: {},
    traces_window: { limit: 200, note: "" }, canaries: [],
  };
}

let posts: string[] = [];

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  posts = [];
  vi.spyOn(window, "confirm").mockReturnValue(false);
});

describe("候选卡片的门禁就绪度", () => {
  it("0 条用例时明说门禁评不了、且候选未被否证", async () => {
    stubFetch(payload([candidate()]), posts);
    render(<SkillsView />);
    const el = await screen.findByTestId("gate-unavailable-invoice-issuance");
    expect(el.textContent).toMatch(/评不了/);
    // 关键:不能让人以为是候选质量问题
    expect(el.textContent).toMatch(/未被否证/);
    // 关键:说清自动化不会推进它
    expect(el.textContent).toMatch(/自动化不会上线|人工放行/);
  });

  it("用例过少时提示证据强度不足,而不是显示成正常", async () => {
    stubFetch(payload([
      candidate({ name: "process-return", is_improvement: true, gate_cases: 1,
                  gate_evaluable: true, gate_underpowered: true }),
    ]), posts);
    render(<SkillsView />);
    const el = await screen.findByTestId("gate-underpowered-process-return");
    expect(el.textContent).toMatch(/1 条用例/);
    expect(el.textContent).toMatch(/噪声|证据强度/);
  });

  it("用例充足时只平铺数量,不加免责声明", async () => {
    stubFetch(payload([
      candidate({ name: "track-order", gate_cases: 5,
                  gate_evaluable: true, gate_underpowered: false }),
    ]), posts);
    render(<SkillsView />);
    const el = await screen.findByTestId("gate-ready-track-order");
    expect(el.textContent).toMatch(/5 条/);
    expect(el.textContent).not.toMatch(/噪声|评不了/);
  });
});

/** 自动合成的门禁用例解开了"新 skill 永远转不了正"的死结,代价是引入了一种
 * 新的骗法:一个「门禁用例 5 条」的候选,如果那 5 条全是机器从真实会话造的、
 * 没有人看过一眼,它与 5 条人工用例的证据强度完全不是一回事——而界面上长得
 * 一模一样。这组测试钉的就是"界面不许把这两者渲染成同一个东西"。 */
describe("合成用例必须与人工用例分开显示", () => {
  it("全是合成用例时明说没有任何人工用例", async () => {
    stubFetch(payload([
      candidate({ name: "draft-outreach-campaign", gate_cases: 5,
                  gate_evaluable: true, gate_underpowered: false,
                  gate_human_cases: 0, gate_synthetic_cases: 5 }),
    ]), posts);
    render(<SkillsView />);
    const el = await screen.findByTestId("gate-synthetic-draft-outreach-campaign");
    expect(el.textContent).toMatch(/5 条自动合成/);
    expect(el.textContent).toMatch(/未经人工审核/);
    expect(el.textContent).toMatch(/无任何人工用例/);
  });

  it("人工+合成混合时两个数字都给出来", async () => {
    stubFetch(payload([
      candidate({ name: "track-order", gate_cases: 6, gate_evaluable: true,
                  gate_underpowered: false,
                  gate_human_cases: 1, gate_synthetic_cases: 5 }),
    ]), posts);
    render(<SkillsView />);
    const el = await screen.findByTestId("gate-synthetic-track-order");
    expect(el.textContent).toMatch(/5 条自动合成/);
    expect(el.textContent).toMatch(/人工 1 条/);
  });

  it("样本不足与「没人审过」是两件独立的坏消息,不能报了前者就吞掉后者", async () => {
    stubFetch(payload([
      candidate({ name: "query-coupons", gate_cases: 2, gate_evaluable: true,
                  gate_underpowered: true,
                  gate_human_cases: 0, gate_synthetic_cases: 2 }),
    ]), posts);
    render(<SkillsView />);
    const under = await screen.findByTestId("gate-underpowered-query-coupons");
    expect(under.textContent).toMatch(/噪声|证据强度/);
    expect(screen.getByTestId("gate-synthetic-query-coupons").textContent)
      .toMatch(/未经人工审核/);
  });

  it("全是人工用例时不出现这一行,免得警告变成噪声", async () => {
    stubFetch(payload([
      candidate({ name: "process-return", gate_cases: 4, gate_evaluable: true,
                  gate_underpowered: false,
                  gate_human_cases: 4, gate_synthetic_cases: 0 }),
    ]), posts);
    render(<SkillsView />);
    await screen.findByTestId("gate-ready-process-return");
    expect(screen.queryByTestId("gate-synthetic-process-return")).toBeNull();
  });

  it("老后端没给这两个字段时不猜、不显示", async () => {
    stubFetch(payload([
      candidate({ name: "track-order", gate_cases: 5, gate_evaluable: true,
                  gate_underpowered: false }),
    ]), posts);
    render(<SkillsView />);
    await screen.findByTestId("gate-ready-track-order");
    expect(screen.queryByTestId("gate-synthetic-track-order")).toBeNull();
  });

  it("字段缺失(老后端/数不出来)时不显示,不编造", async () => {
    stubFetch(payload([
      candidate({ gate_cases: null, gate_evaluable: null,
                  gate_underpowered: null, gate_note: null }),
    ]), posts);
    render(<SkillsView />);
    await screen.findByText("invoice-issuance");
    expect(screen.queryByTestId("gate-unavailable-invoice-issuance")).toBeNull();
    expect(screen.queryByTestId("gate-ready-invoice-issuance")).toBeNull();
  });
});

describe("转正确认框", () => {
  it("门禁评不了时说明人工放行是唯一通路", async () => {
    stubFetch(payload([candidate()]), posts);
    const spy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<SkillsView />);
    fireEvent.click(await screen.findByTestId("promote-invoice-issuance"));
    const msg = String(spy.mock.calls[0]?.[0] ?? "");
    expect(msg).toMatch(/评不了/);
    expect(msg).toMatch(/唯一通路/);
    expect(posts).toHaveLength(0);
  });

  it("门禁能评时仍是原来那句「本次不跑门禁」,不混成同一种说法", async () => {
    stubFetch(payload([
      candidate({ name: "track-order", gate_cases: 5,
                  gate_evaluable: true, gate_underpowered: false }),
    ]), posts);
    const spy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<SkillsView />);
    fireEvent.click(await screen.findByTestId("promote-track-order"));
    const msg = String(spy.mock.calls[0]?.[0] ?? "");
    expect(msg).toMatch(/本次\*\*不跑评测门禁\*\*|不跑评测门禁/);
    expect(msg).not.toMatch(/唯一通路/);
  });

  it("确认后仍走 force=true(界面这条路一直如此,本次改动没动它)", async () => {
    stubFetch(payload([candidate()]), posts);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<SkillsView />);
    fireEvent.click(await screen.findByTestId("promote-invoice-issuance"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatch(/invoice-issuance\/promote/);
    expect(posts[0]).toMatch(/force=true/);
  });
});
