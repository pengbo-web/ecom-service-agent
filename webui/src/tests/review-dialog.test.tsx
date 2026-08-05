import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ReviewDialog } from "@/components/ReviewDialog";
import { OrdersView } from "@/components/OrdersView";
import type { ReviewableItem } from "@/lib/api";

const ITEM: ReviewableItem = {
  order_id: "O1", sku: "P001", name: "跑鞋", delivered_at: "2024-01-02 10:00:00",
};

describe("ReviewDialog", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("未选星不能提交", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ success: true }) })));
    render(<ReviewDialog item={ITEM} open onOpenChange={() => {}} onSubmitted={() => {}} />);
    const submit = await screen.findByRole("button", { name: /提交评价/ });
    expect(submit).toBeDisabled();

    fireEvent.click(submit);
    // 按钮被禁用,原生 click 不会触发处理函数,不该有任何 POST 打出去
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.some((c) => (c[1] as RequestInit | undefined)?.method === "POST")).toBe(false);
  });

  it("选星后可以提交", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ success: true, review_id: 1 }) })));
    const onSubmitted = vi.fn();
    render(<ReviewDialog item={ITEM} open onOpenChange={() => {}} onSubmitted={onSubmitted} />);
    fireEvent.click(await screen.findByTestId("star-4"));
    expect(await screen.findByRole("button", { name: /提交评价/ })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: /提交评价/ }));
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith(ITEM));

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    const postCall = calls.find((c) => (c[1] as RequestInit | undefined)?.method === "POST");
    expect(postCall).toBeTruthy();
    const body = JSON.parse((postCall![1] as RequestInit).body as string);
    expect(body).toEqual({ order_id: "O1", sku: "P001", rating: 4, content: "" });
  });

  it("重复评价时把后端原文显示出来,而不是自己改写措辞", async () => {
    const reason = "这笔订单的该商品已经评价过了,不能重复评价。";
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ success: false, reason }) })));
    render(<ReviewDialog item={ITEM} open onOpenChange={() => {}} onSubmitted={() => {}} />);
    fireEvent.click(await screen.findByTestId("star-3"));
    fireEvent.click(screen.getByRole("button", { name: /提交评价/ }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(reason);
  });

  it("网络/鉴权失败也不能让弹窗静默无反应", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })));
    render(<ReviewDialog item={ITEM} open onOpenChange={() => {}} onSubmitted={() => {}} />);
    fireEvent.click(await screen.findByTestId("star-5"));
    fireEvent.click(screen.getByRole("button", { name: /提交评价/ }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});

describe("OrdersView + ReviewDialog 集成:提交成功后按钮变已评价", () => {
  const ORDERS_BODY = {
    orders: [{
      order_id: "O1", status: "delivered", status_label: "已签收",
      items: [{ name: "跑鞋", sku: "P001", quantity: 1, price: 899 }],
      total: 899, created_at: "2024-01-01 10:00:00", shipping_address: "",
    }],
  };
  const REVIEWABLE_BODY = { success: true, items: [ITEM] };

  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  function stub(reviewResult: unknown) {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (init?.method === "POST" && u.includes("/api/review")) {
        return { ok: true, json: async () => reviewResult };
      }
      if (u.includes("/api/reviewable")) return { ok: true, json: async () => REVIEWABLE_BODY };
      if (u.includes("/api/orders")) return { ok: true, json: async () => ORDERS_BODY };
      return { ok: true, json: async () => ({}) };
    }));
  }

  it("已签收未评价的商品显示「评价」按钮,提交成功后变成「已评价」", async () => {
    stub({ success: true, review_id: 1 });
    render(<OrdersView onShop={() => {}} />);

    const reviewBtn = await screen.findByRole("button", { name: "评价" });
    fireEvent.click(reviewBtn);

    fireEvent.click(await screen.findByTestId("star-5"));
    fireEvent.click(screen.getByRole("button", { name: /提交评价/ }));

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "评价" })).not.toBeInTheDocument();
    });
    expect(await screen.findByText("已评价")).toBeInTheDocument();
  });

  it("重复评价提交失败:按钮仍保留「评价」态(不能乐观地当成功处理)", async () => {
    stub({ success: false, reason: "这笔订单的该商品已经评价过了,不能重复评价。" });
    render(<OrdersView onShop={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: "评价" }));
    fireEvent.click(await screen.findByTestId("star-2"));
    fireEvent.click(screen.getByRole("button", { name: /提交评价/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/已经评价过了/);
    // 没有真正提交成功,不该出现"已评价"这个终态文案(弹窗仍打开着,底层
    // 「评价」按钮被 Radix 标成 aria-hidden 不便再用 getByRole 查,
    // 故只断言"已评价"三个字确实没有出现)
    expect(screen.queryByText("已评价")).not.toBeInTheDocument();
  });
});
