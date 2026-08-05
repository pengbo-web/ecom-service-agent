import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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

  it("命中承诺词的草稿必须在视觉上明显区别于未命中的草稿,而不只是有文字", async () => {
    const CLEAN = { ...DRAFT, id: 3, user_id: "u3" };
    const FLAGGED2 = { ...FLAGGED, id: 4, user_id: "u4" };
    stub([CLEAN, FLAGGED2]);
    render(<GrowthPanel />);

    const cleanCard = await screen.findByTestId("draft-3");
    const flaggedCard = await screen.findByTestId("draft-4");

    // 未命中的草稿卡片不应该带任何 destructive 强调样式
    expect(cleanCard.className).not.toMatch(/border-destructive/);
    // 命中的草稿卡片必须带醒目的强调边框——这是与"普通草稿"的视觉区分点
    expect(flaggedCard.className).toMatch(/border-destructive/);

    // 原因文案本身也必须用醒目样式渲染,不能只是普通文本——删掉标红样式
    // 而只留文字的话,这条断言应该失败
    const reasonEl = within(flaggedCard).getByText(/金钱承诺词/);
    expect(reasonEl.className).toMatch(/text-destructive/);
    expect(within(cleanCard).queryByText(/金钱承诺词/)).not.toBeInTheDocument();
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

  it("投递成功但账本记录失败——草稿摘出待审队列,原因必须显眼展示,不能读成普通成功", async () => {
    const reason = "消息已投递给买家,但标记为已发送时失败;请人工核查草稿 1 的状态";
    stub([DRAFT], { success: false, sent: true, reason });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /批准/ }));

    // 原因必须出现,而且要用醒目的 alert 语义/样式展示,不是随手一行小字
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/人工核查/);
    expect(alert.className).toMatch(/text-destructive/);

    // 已投递 = 草稿必须从待审队列摘掉(不可能再退回去重新批准一次)
    await waitFor(() => {
      expect(screen.queryByText(/尺码建议/)).not.toBeInTheDocument();
    });
  });

  it("两行同时批准互不干扰——一行完成不能解锁另一行仍在处理中的按钮", async () => {
    const DRAFT_A = { ...DRAFT, id: 1, user_id: "uA" };
    const DRAFT_B = { ...DRAFT, id: 2, user_id: "uB", content: "第二条草稿正文" };
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const resolvers: Record<string, () => void> = {};
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const m = String(url).match(/drafts\/(\d+)\/approve/);
        const id = m![1];
        return new Promise((resolve) => {
          resolvers[id] = () =>
            resolve({ ok: true, json: async () => ({ success: true, sent: true, reason: "" }) });
        });
      }
      return { ok: true, json: async () => ({ success: true, drafts: [DRAFT_A, DRAFT_B] }) };
    }));

    render(<GrowthPanel />);
    const approveA = await screen.findByTestId("approve-1");
    const approveB = await screen.findByTestId("approve-2");

    fireEvent.click(approveA);
    fireEvent.click(approveB);

    // 两行都应该已经进入处理中、按钮禁用
    await waitFor(() => {
      expect(approveA).toBeDisabled();
      expect(approveB).toBeDisabled();
    });

    // 先让 B 完成(先点确认/先返回的不一定是先点的那行)
    resolvers["2"]();
    await waitFor(() => {
      expect(screen.queryByTestId("draft-2")).not.toBeInTheDocument();
    });

    // 关键断言:B 完成之后,A 仍在处理中,按钮必须仍然禁用——
    // 如果 busy 状态是单个标量,这里会被 B 的 resolve 错误地清空
    expect(screen.getByTestId("approve-1")).toBeDisabled();
    expect(screen.getByTestId("draft-1")).toBeInTheDocument();

    // 再让 A 完成,确认它自己也能正常收尾
    resolvers["1"]();
    await waitFor(() => {
      expect(screen.queryByTestId("draft-1")).not.toBeInTheDocument();
    });
  });

  it("空态给明确文案", async () => {
    stub([]);
    render(<GrowthPanel />);
    expect(await screen.findByText(/暂无待审草稿/)).toBeInTheDocument();
  });

  it("草稿卡片显示商机类型的中文标签(后端给出),而不是原始标识符", async () => {
    // opportunity_label 由后端(growth_drafts 端点,与 OPPORTUNITY_KINDS 同源)
    // 附带给出——前端不再自己维护一份 kind→label 映射表去猜。
    const labeled = { ...DRAFT, opportunity_label: "下单后久未推进(已付款待发货)" };
    stub([labeled]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("draft-1");
    expect(within(card).getByText("下单后久未推进(已付款待发货)")).toBeInTheDocument();
    // 原始标识符不该再原样展示给店主
    expect(within(card).queryByText("stale_pending_order")).not.toBeInTheDocument();
  });

  it("未知的商机类型标签兜底显示原始标识符,而不是留空", async () => {
    const UNKNOWN = { ...DRAFT, id: 9, opportunity_type: "brand_new_kind" };
    stub([UNKNOWN]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("draft-9");
    expect(within(card).getByText("brand_new_kind")).toBeInTheDocument();
  });

  it("同一批扫描里多张草稿共享的整段来源理由默认折叠,不逐条铺开长段落", async () => {
    const LONG_REASON = "这是一段很长的店铺诊断原文,包含多项经营指标与多条建议,足够长到需要折叠展示给店主看。";
    const A = { ...DRAFT, id: 5, user_id: "uA", reason: LONG_REASON };
    const B = { ...DRAFT, id: 6, user_id: "uB", reason: LONG_REASON };
    stub([A, B]);
    render(<GrowthPanel />);
    const cardA = await screen.findByTestId("draft-5");

    // 折叠态:完整长段落不应该整段出现在 DOM 里
    expect(within(cardA).queryByText(LONG_REASON)).not.toBeInTheDocument();
    // 但要留一个展开入口,理由本身没有被丢弃
    const toggle = within(cardA).getByTestId("reason-toggle-5");
    expect(toggle).toHaveTextContent("展开");

    fireEvent.click(toggle);
    // 展开后完整文本可见,且该卡片自己的按钮变成"收起"
    expect(within(cardA).getByText(new RegExp(LONG_REASON))).toBeInTheDocument();
    expect(within(cardA).getByTestId("reason-toggle-5")).toHaveTextContent("收起");

    // 另一张卡片(同样的长理由)默认仍是折叠态,互不影响
    const cardB = await screen.findByTestId("draft-6");
    expect(within(cardB).getByTestId("reason-toggle-6")).toHaveTextContent("展开");
  });

  it("较短的来源理由不需要折叠交互,直接原样显示", async () => {
    const SHORT = { ...DRAFT, id: 7, reason: "未付款" };
    stub([SHORT]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("draft-7");
    expect(within(card).getByText(/未付款/)).toBeInTheDocument();
    expect(within(card).queryByTestId("reason-toggle-7")).not.toBeInTheDocument();
  });

  it("商机概览的分类与文案完全来自后端接口,前端不能再抄一份固定列表", async () => {
    // 后端下发一个前端从未见过、代码里任何地方都不会硬编码的 kind——
    // 如果前端又倒退回一份写死的 OPPORTUNITY_KINDS 列表,这里必然渲染不出来。
    const NEW_KIND = "brand_new_kind_6";
    const NEW_LABEL = "全新第六类商机(后端刚加的)";
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return { ok: true, json: async () => ({ success: true, sent: true, reason: "" }) };
      const u = String(url);
      if (u.includes("opportunity-kinds")) {
        return { ok: true, json: async () => ({ success: true, kinds: [{ kind: NEW_KIND, label: NEW_LABEL }] }) };
      }
      if (u.includes("/opportunities")) {
        return {
          ok: true,
          json: async () => ({
            success: true, kind: NEW_KIND, kind_label: NEW_LABEL, window_days: 14,
            count: 7, opportunities: [],
          }),
        };
      }
      if (u.includes("outreach-stats")) {
        return {
          ok: true,
          json: async () => ({ success: true, window_days: 30, window_hours: 24, sent: 0, converted: 0, conversion_rate: 0 }),
        };
      }
      return { ok: true, json: async () => ({ success: true, drafts: [] }) };
    }));

    render(<GrowthPanel />);

    // 服务端下发的中文标签必须原样出现——不是从任何前端映射表拼出来的
    expect(await screen.findByText(NEW_LABEL)).toBeInTheDocument();
    // 对应计数(来自 /opportunities 的 count)也要渲染出来
    expect(await screen.findByText("7")).toBeInTheDocument();
    // 标识符本身不该直接展示给店主
    expect(screen.queryByText(NEW_KIND)).not.toBeInTheDocument();
  });
});
