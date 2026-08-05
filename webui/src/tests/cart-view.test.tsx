import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { CartView } from "@/components/CartView";
import { OrdersView } from "@/components/OrdersView";
import type { CartItem } from "@/lib/api";

// 用一个可变数组模拟后端购物车状态,让 GET /api/cart 在 POST/DELETE 之后
// 能读到最新结果——与真实后端"写后读一致"的行为一致,而不是每次都回同一份快照。
function stubCart(initial: CartItem[]) {
  const items: CartItem[] = initial.map((i) => ({ ...i }));
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method || "GET";
    if (u === "/api/cart" && method === "GET") {
      return { ok: true, json: async () => ({ success: true, items: [...items] }) };
    }
    if (u === "/api/cart" && method === "POST") {
      const body = JSON.parse(init!.body as string);
      const existing = items.find((it) => it.sku === body.item_id);
      if (existing) existing.quantity += body.quantity;
      else items.push({ id: items.length + 1, user_id: "u1", sku: body.item_id,
        quantity: body.quantity, added_at: "t", status: "active" });
      return { ok: true, json: async () => ({ success: true }) };
    }
    if (u.startsWith("/api/cart/") && method === "DELETE") {
      const sku = decodeURIComponent(u.replace("/api/cart/", ""));
      const idx = items.findIndex((it) => it.sku === sku);
      if (idx >= 0) items.splice(idx, 1);
      return { ok: true, json: async () => ({ success: true }) };
    }
    return { ok: true, json: async () => ({}) };
  }));
}

describe("CartView", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("空购物车显示中文空态", async () => {
    stubCart([]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByText("购物车空空如也，去商城逛逛吧～")).toBeInTheDocument();
  });

  it("加购后件数更新", async () => {
    stubCart([{ id: 1, user_id: "u1", sku: "P001", quantity: 1, added_at: "t", status: "active" }]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByText("共 1 件")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "增加数量" }));
    await waitFor(() => expect(screen.getByText("共 2 件")).toBeInTheDocument());

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    const postCall = calls.find((c) => (c[1] as RequestInit | undefined)?.method === "POST");
    expect(postCall).toBeTruthy();
    expect(JSON.parse((postCall![1] as RequestInit).body as string)).toEqual({ item_id: "P001", quantity: 1 });
  });

  it("移除后该商品从列表消失", async () => {
    stubCart([{ id: 1, user_id: "u1", sku: "P001", quantity: 2, added_at: "t", status: "active" }]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByText("P001")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: /移除/ }));

    await waitFor(() => expect(screen.queryByText("P001")).not.toBeInTheDocument());
    expect(await screen.findByText("购物车空空如也，去商城逛逛吧～")).toBeInTheDocument();
  });
});

describe("OrdersView「去支付」:调对端点且成功后状态文案变化", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  const UNPAID_ORDER = {
    order_id: "O1", status: "unpaid", status_label: "待支付",
    items: [{ name: "跑鞋", sku: "P001", quantity: 1, price: 899 }],
    total: 899, created_at: "2024-01-01 10:00:00", shipping_address: "",
  };
  const PAID_ORDER = { ...UNPAID_ORDER, status: "pending", status_label: "待发货" };

  function stubOrders() {
    let paid = false;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/api/reviewable")) return { ok: true, json: async () => ({ success: true, items: [] }) };
      if (u.includes("/pay") && init?.method === "POST") {
        paid = true;
        return { ok: true, json: async () => ({ success: true, status: "pending", status_label: "待发货" }) };
      }
      if (u.includes("/api/orders")) {
        return { ok: true, json: async () => ({ orders: [paid ? PAID_ORDER : UNPAID_ORDER] }) };
      }
      return { ok: true, json: async () => ({}) };
    }));
  }

  it("待支付订单显示「去支付」,点击后调用支付端点,刷新后状态变为待发货", async () => {
    stubOrders();
    render(<OrdersView onShop={() => {}} />);

    expect(await screen.findByText("待支付")).toBeInTheDocument();
    const payBtn = await screen.findByRole("button", { name: "去支付" });

    fireEvent.click(payBtn);

    await waitFor(() => expect(screen.getByText("待发货")).toBeInTheDocument());
    expect(screen.queryByText("待支付")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "去支付" })).not.toBeInTheDocument();

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    const payCall = calls.find((c) => String(c[0]).includes("/pay"));
    expect(payCall).toBeTruthy();
    expect((payCall![1] as RequestInit).method).toBe("POST");
  });
});
