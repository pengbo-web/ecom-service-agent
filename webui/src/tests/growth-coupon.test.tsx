import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { GrowthPanel } from "@/components/operations/GrowthPanel";

// N6:批准草稿时若 offer.coupon_code 非空,会真的发放一张优惠券——碰钱、
// 不可撤销,必须在人工点下批准**之前**就在界面上说清楚,而不是发完了才
// 在结果里告知。coupon_discount 由后端(growth_drafts 端点,与
// app.agent.coupons.grants.COUPON_BY_CODE 同源)给出,前端不再自己维护
// 一份券码/文案表。
const DRAFT_WITH_COUPON = {
  id: 1, opportunity_type: "unpaid_order", user_id: "u1", order_id: "O1",
  content: "亲,给您留了一张券,记得用哦", offer: { coupon_code: "SHOE30" },
  reason: "催付款", correlation_id: "C1", status: "draft", needs_review_reason: "",
  created_by: "growth", reviewed_by: null, created_at: "2026-08-05 10:00:00",
  coupon_discount: "满300减30",
};

const DRAFT_NO_COUPON = {
  ...DRAFT_WITH_COUPON, id: 2, offer: {}, coupon_discount: undefined,
};

const DRAFT_UNKNOWN_COUPON = {
  ...DRAFT_WITH_COUPON, id: 3, offer: { coupon_code: "MODEL_MADE_THIS_UP" },
  coupon_discount: "",
};

function stub(drafts: unknown[], approveBody: unknown = { success: true, sent: true }) {
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") return { ok: true, json: async () => approveBody };
    return { ok: true, json: async () => ({ success: true, drafts }) };
  }));
}

describe("GrowthPanel · 优惠券发放(N6)", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("带券草稿显著显示券码与券面文案", async () => {
    stub([DRAFT_WITH_COUPON]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("draft-1");
    expect(within(card).getByText(/SHOE30/)).toBeInTheDocument();
    expect(within(card).getByText(/满300减30/)).toBeInTheDocument();
  });

  it("不带券的草稿不显示优惠券横条", async () => {
    stub([DRAFT_NO_COUPON]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("draft-2");
    expect(within(card).queryByTestId("coupon-2")).not.toBeInTheDocument();
  });

  it("批准带券草稿前,二次确认文案必须点名发放哪张券", async () => {
    stub([DRAFT_WITH_COUPON]);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByTestId("approve-1"));
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
    const message = confirmSpy.mock.calls[0][0] as string;
    expect(message).toMatch(/优惠券/);
    expect(message).toMatch(/SHOE30/);
  });

  it("不带券草稿的确认文案不提及优惠券——不能无中生有暗示发券", async () => {
    stub([DRAFT_NO_COUPON]);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByTestId("approve-2"));
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
    const message = confirmSpy.mock.calls[0][0] as string;
    expect(message).not.toMatch(/优惠券/);
  });

  it("取消确认框不会发出批准请求,券也不会被发放", async () => {
    stub([DRAFT_WITH_COUPON]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByTestId("approve-1"));
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.some((c) => (c[1] as RequestInit | undefined)?.method === "POST")).toBe(false);
  });

  it("券码不是店铺已知券时提示可能发放失败,而不是假装是一张真实的券", async () => {
    stub([DRAFT_UNKNOWN_COUPON]);
    render(<GrowthPanel />);
    const card = await screen.findByTestId("draft-3");
    expect(within(card).getByText(/MODEL_MADE_THIS_UP/)).toBeInTheDocument();
    expect(within(card).getByText(/可能发放失败/)).toBeInTheDocument();
  });

  it("发券失败(与投递失败同一处理)时,原因要显示且草稿留在待审队列", async () => {
    stub([DRAFT_WITH_COUPON],
      { success: false, sent: false, reason: "发券失败:券码「SHOE30」不是本店在售的优惠券,已拒绝发放,已退回待审;直接重试不会成功(原因不会变),需人工处理后再批准" });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<GrowthPanel />);
    fireEvent.click(await screen.findByTestId("approve-1"));
    expect(await screen.findByText(/发券失败/)).toBeInTheDocument();
    // 未真正投递 → 草稿必须还在待审队列里,不能乐观移除
    expect(screen.getByTestId("draft-1")).toBeInTheDocument();
  });
});
