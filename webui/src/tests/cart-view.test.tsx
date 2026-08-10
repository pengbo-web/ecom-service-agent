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
    if (u.startsWith("/api/cart/") && method === "PUT") {
      const sku = decodeURIComponent(u.replace("/api/cart/", ""));
      const body = JSON.parse(init!.body as string);
      const existing = items.find((it) => it.sku === sku);
      if (existing) existing.quantity = body.quantity;
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

  it("加购后件数更新(增/减都走同一个设置数量端点)", async () => {
    stubCart([{ id: 1, user_id: "u1", sku: "P001", quantity: 1, added_at: "t", status: "active" }]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByText("共 1 件")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "增加数量" }));
    await waitFor(() => expect(screen.getByText("共 2 件")).toBeInTheDocument());

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    const putCall = calls.find((c) => (c[1] as RequestInit | undefined)?.method === "PUT");
    expect(putCall).toBeTruthy();
    expect(String(putCall![0])).toBe("/api/cart/P001");
    expect(JSON.parse((putCall![1] as RequestInit).body as string)).toEqual({ quantity: 2 });

    // 减量同样走 PUT(而不是先 DELETE 再 POST 这种两次往返)
    fireEvent.click(await screen.findByRole("button", { name: "减少数量" }));
    await waitFor(() => expect(screen.getByText("共 1 件")).toBeInTheDocument());
    const putCalls = calls.filter((c) => (c[1] as RequestInit | undefined)?.method === "PUT");
    expect(putCalls.length).toBe(2);
    expect(JSON.parse((putCalls[1][1] as RequestInit).body as string)).toEqual({ quantity: 1 });
  });

  it("移除后该商品从列表消失", async () => {
    stubCart([{ id: 1, user_id: "u1", sku: "P001", quantity: 2, added_at: "t", status: "active" }]);
    render(<CartView onShop={() => {}} />);
    // 查不到商品信息时退回「商品 <sku>」,而不是把一个原始 id 当商品名摆着
    expect(await screen.findByText("商品 P001")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: /移除/ }));

    await waitFor(() => expect(screen.queryByText("商品 P001")).not.toBeInTheDocument());
    expect(await screen.findByText("购物车空空如也，去商城逛逛吧～")).toBeInTheDocument();
  });

  // 改造前购物车里一件商品只显示一个原始 sku(如「1」),没有名字、没有价格、
  // 没有小计。最严重的是那个「去下单」按钮:它会在买家**从未看到价格**的情况下
  // 提交订单,下完单才用弹窗告诉他付了多少——那是让人闭着眼睛付钱。
  const PRICED: CartItem = {
    id: 1, user_id: "u1", sku: "1", quantity: 2, added_at: "t", status: "active",
    title: "Nike Air Max 270 运动鞋", price: 899, image: "", stock: 153,
    subtotal: 1798, product_missing: false,
  };

  it("显示商品名、单价、小计,而不是一个原始 sku", async () => {
    stubCart([PRICED]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByText("Nike Air Max 270 运动鞋")).toBeInTheDocument();
    expect(screen.getByText("单价 ¥899")).toBeInTheDocument();
    expect(screen.getByText("¥1798")).toBeInTheDocument();
  });

  it("金额写在下单按钮上,点之前就能看到", async () => {
    stubCart([PRICED]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByRole("button", { name: /去下单 ¥1798/ })).toBeInTheDocument();
  });

  it("顶部给出合计", async () => {
    stubCart([PRICED]);
    render(<CartView onShop={() => {}} />);
    const total = await screen.findByTestId("cart-total");
    expect(total.textContent).toContain("¥1798.00");
  });

  it("商品查不到时明说,且不把它当 ¥0 混进合计", async () => {
    // price=null 渲染成「¥0」会让买家以为免费;把它当 0 计入合计则给出一个
    // 偏低且看不出来的总额。两者都比"说清楚查不到"更糟。
    stubCart([
      PRICED,
      { id: 2, user_id: "u1", sku: "GONE", quantity: 1, added_at: "t", status: "active",
        title: null, price: null, image: null, stock: null,
        subtotal: null, product_missing: true },
    ]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByText(/商品信息暂时查不到/)).toBeInTheDocument();
    const total = await screen.findByTestId("cart-total");
    expect(total.textContent).toContain("¥1798.00");        // 只算得出的那件
    expect(total.textContent).toContain("不含 1 件");
    expect(screen.queryByText("¥0")).toBeNull();
  });

  it("老后端不带商品字段时不崩,退回可用状态", async () => {
    stubCart([{ id: 1, user_id: "u1", sku: "P001", quantity: 1, added_at: "t", status: "active" }]);
    render(<CartView onShop={() => {}} />);
    expect(await screen.findByText("商品 P001")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /去下单/ })).toBeInTheDocument();
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
