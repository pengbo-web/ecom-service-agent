import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    // 断言恰好 2 处:候选段一处 + 灰度段一处。用 >0 会让"候选完全不渲染"也通过
    // (灰度那一处就够满足),等于这条断言失去可失败性。
    expect(await screen.findAllByText("track-order")).toHaveLength(2);
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

describe("SkillsView 蒸馏截断提示", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("后端标记 truncated 时展示截断警告(资料尾部没真正参与蒸馏)", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      // call 1: 初始总览拉取; call 2: 蒸馏请求; call 3: created 后 load() 再次拉取总览
      if (call === 2) return { ok: true, json: async () => ({
        created: true, name: "sop-return", risk: "low", policy: "canary_ab",
        errors: [], truncated: true,
      }) };
      return { ok: true, json: async () => OVERVIEW };
    }));

    render(<SkillsView />);
    await screen.findByText(/上传客服 SOP/);
    fireEvent.change(screen.getByPlaceholderText(/粘贴客服 SOP/), {
      target: { value: "超长资料正文…" },
    });
    fireEvent.click(screen.getByText(/提炼成候选技能/));

    expect(await screen.findByText(/仅前 12000 字参与了蒸馏/)).toBeInTheDocument();
  });

  it("蒸馏请求失败展示在蒸馏卡片里,不是顶部的「读取失败」", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 1) return { ok: true, json: async () => OVERVIEW };
      return { ok: false, status: 500, json: async () => ({}) };
    }));

    render(<SkillsView />);
    await screen.findByText(/上传客服 SOP/);
    fireEvent.change(screen.getByPlaceholderText(/粘贴客服 SOP/), {
      target: { value: "资料正文" },
    });
    fireEvent.click(screen.getByText(/提炼成候选技能/));

    expect(await screen.findByText(/提炼请求失败/)).toBeInTheDocument();
    expect(screen.queryByText(/读取失败/)).toBeNull();
  });
});

describe("SkillsView 刷新失败与卡片独立状态", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("刷新失败时旧数据必须被标成已过期(不能默默照常渲染)", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 1) return { ok: true, json: async () => OVERVIEW };
      return { ok: false, status: 500, json: async () => ({}) };   // 刷新失败
    }));

    render(<SkillsView />);
    await screen.findByText("process-return");

    fireEvent.click(screen.getByText(/刷新/));

    // 旧数据仍在(不清空是刻意的),但必须明确标出它已过期
    expect(await screen.findByText("数据已过期")).toBeInTheDocument();
    expect(await screen.findByText(/上次成功刷新时的旧数据/)).toBeInTheDocument();
    expect(screen.getByText("process-return")).toBeInTheDocument();
  });

  it("提炼进行中不得把上传卡片也渲染成「上传中…」", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 1) return { ok: true, json: async () => OVERVIEW };
      return new Promise(() => {});          // 蒸馏请求一直挂着
    }));

    render(<SkillsView />);
    await screen.findByText(/上传客服 SOP/);
    fireEvent.change(screen.getByPlaceholderText(/粘贴客服 SOP/), {
      target: { value: "资料正文" },
    });
    fireEvent.click(screen.getByText(/提炼成候选技能/));

    expect(await screen.findByText("提炼中…")).toBeInTheDocument();
    expect(screen.queryByText("上传中…")).toBeNull();
  });

  it("刷新会清掉上一次的提炼结论(旧的红色「未通过」不该跨动作残留)", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 2) return { ok: true, json: async () => ({
        created: false, name: null, risk: null, policy: null,
        errors: ["LLM 产物未通过校验(frontmatter 不全 / 工具名不实 / 名字非法)"],
        truncated: false,
      }) };
      return { ok: true, json: async () => OVERVIEW };
    }));

    render(<SkillsView />);
    await screen.findByText(/上传客服 SOP/);
    fireEvent.change(screen.getByPlaceholderText(/粘贴客服 SOP/), {
      target: { value: "资料正文" },
    });
    fireEvent.click(screen.getByText(/提炼成候选技能/));
    expect(await screen.findByText(/LLM 产物未通过校验/)).toBeInTheDocument();

    fireEvent.click(screen.getByText(/刷新/));

    await waitFor(() => expect(screen.queryByText(/LLM 产物未通过校验/)).toBeNull());
  });
});
