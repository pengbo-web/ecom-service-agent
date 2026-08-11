import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { GrowthPanel } from "@/components/operations/GrowthPanel";

/** 不可投递的草稿要在**批准之前**标出来。
 *
 * 实测:给一个从没聊过天的买家造了订单,商机发现器照常挑出他 → 营销花一次 LLM
 * 起草 → 人工审完点批准 → 才发现送不出去(投递通道是"追加进买家自己的客服会话")。
 * 而且当时的提示是"投递失败,已退回待审,可重试"——重试永远失败。
 *
 * 与那张券的提示同一条原则:把"批准之后会发生什么"在按钮按下之前摆出来。
 * 真实数据上量到 9 条待审里 4 条(44%)结构上不可投递。
 */
const BASE = {
  id: 1, opportunity_type: "stale_pending_order", opportunity_label: "下单后久未推进",
  user_id: "no_session_buyer", order_id: "O-1", content: "您好呀～您这单还在待发货",
  offer: {}, reason: "下单后久未推进 · 滞留 212h", correlation_id: "C-1",
  status: "draft", needs_review_reason: "", created_by: "growth",
  reviewed_by: null, created_at: "2026-08-11 10:00:00",
};

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getGrowthDrafts: vi.fn(),
    getOpportunityKinds: vi.fn(async () => ({ kinds: [] })),
    getGrowthHealth: vi.fn(async () => ({})),
    getFollowups: vi.fn(async () => ({ followups: [] })),
  };
});

beforeEach(() => { vi.clearAllMocks(); });

async function renderWith(drafts: any[]) {
  const api = await import("@/lib/api");
  (api.getGrowthDrafts as any).mockResolvedValue({ drafts });
  render(<GrowthPanel />);
  await waitFor(() => expect(api.getGrowthDrafts).toHaveBeenCalled());
}

describe("草稿的可投递性标注", () => {
  it("不可投递时显示原因，且说明重试也没用", async () => {
    await renderWith([{
      ...BASE, deliverable: false,
      undeliverable_reason: "该买家没有客服会话,批准后无法投递(重试也不会成功)",
    }]);
    const el = await screen.findByTestId("undeliverable-1");
    expect(el.textContent).toContain("没有客服会话");
    expect(el.textContent).toContain("重试也不会成功");
  });

  it("可投递时不显示这块，不给审批队列加噪声", async () => {
    await renderWith([{ ...BASE, deliverable: true, undeliverable_reason: "" }]);
    await screen.findByTestId("draft-1");
    expect(screen.queryByTestId("undeliverable-1")).toBeNull();
  });

  it("老响应体没有这两个键时按可达处理，不凭空标红", async () => {
    await renderWith([BASE]);
    await screen.findByTestId("draft-1");
    expect(screen.queryByTestId("undeliverable-1")).toBeNull();
  });

  it("不可投递也仍然渲染草稿本身——只标注不隐藏，处置权归人", async () => {
    await renderWith([{
      ...BASE, deliverable: false, undeliverable_reason: "该买家没有客服会话",
    }]);
    expect(await screen.findByTestId("draft-1")).toBeInTheDocument();
    expect(screen.getByText(/您这单还在待发货/)).toBeInTheDocument();
  });
});
