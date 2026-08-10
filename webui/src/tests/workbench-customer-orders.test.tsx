import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { ContextPanel } from "@/components/workbench/ContextPanel";
import type { WbConversation } from "@/lib/api";

// 坐席台右侧客户面板此前只有会话元数据(会话 ID / 轮次 / 最后活跃),
// **接待人看不到客户买了什么**。而买家开口第一句几乎总是关于某一笔订单——
// 坐席只能反问"您的订单号是多少",把 AI 已经知道的事情重新问一遍人。

const CONV: WbConversation = {
  conversation_id: "c-1", user_id: "u1", status: "open",
  created_at: "2026-08-10 10:00:00", last_active: "2026-08-10 10:05:00",
  manual: false, preview: "你好", turns: 3,
};

const ORDERS = [
  { order_id: "ORD-1", status: "refunding", status_label: "退款处理中",
    items: [{ name: "Nike 运动鞋", sku: "S1", quantity: 1, price: 899 }],
    total: 899, created_at: "2026-08-01 10:00:00", shipping_address: "上海" },
  { order_id: "ORD-2", status: "delivered", status_label: "已签收",
    items: [{ name: "小米14", sku: "S2", quantity: 1, price: 5999 }],
    total: 5999, created_at: "2026-07-20 10:00:00", shipping_address: "上海" },
];

function stub(payload: unknown, ok = true) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (String(url).includes("/orders")) {
      return { ok, status: ok ? 200 : 500, json: async () => payload };
    }
    return { ok: true, json: async () => ({}) };
  }));
}

describe("坐席客户面板 · 订单", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("列出该客户的订单", async () => {
    stub({ success: true, user_id: "u1", orders: ORDERS, degraded: "" });
    render(<ContextPanel conv={CONV} />);
    const panel = await screen.findByTestId("customer-orders");
    expect(await within(panel).findByTestId("cust-order-ORD-1")).toBeInTheDocument();
    expect(within(panel).getByText("退款处理中")).toBeInTheDocument();
    expect(within(panel).getByText("2 笔")).toBeInTheDocument();
  });

  it("服务降级时明确标出,不能显示成「该客户暂无订单」", async () => {
    // 把"订单服务连不上"渲染成"暂无订单",坐席会据此对买家说"您名下没有订单"
    // ——那是一句由前端渲染逻辑造出来的假话。
    stub({ success: false, user_id: "u1", orders: [], degraded: "订单服务不可用(ReadTimeout)" });
    render(<ContextPanel conv={CONV} />);
    const panel = await screen.findByTestId("customer-orders");
    expect(await within(panel).findByText(/ReadTimeout/)).toBeInTheDocument();
    expect(within(panel).queryByText("该客户名下暂无订单")).toBeNull();
  });

  it("确实没订单时才说暂无", async () => {
    stub({ success: true, user_id: "u1", orders: [], degraded: "" });
    render(<ContextPanel conv={CONV} />);
    const panel = await screen.findByTestId("customer-orders");
    expect(await within(panel).findByText("该客户名下暂无订单")).toBeInTheDocument();
  });

  it("切客户时先清空,不残留上一个客户的订单", async () => {
    // 残留会让坐席对着 A 的单跟 B 说话,比看不到订单严重得多。
    stub({ success: true, user_id: "u1", orders: ORDERS, degraded: "" });
    const { rerender } = render(<ContextPanel conv={CONV} />);
    await screen.findByTestId("cust-order-ORD-1");

    let resolveSecond: (v: unknown) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise((r) => { resolveSecond = r; })));
    rerender(<ContextPanel conv={{ ...CONV, conversation_id: "c-2", user_id: "u2" }} />);

    // 新客户的请求还没回来时,旧客户的订单必须已经不在
    await waitFor(() => expect(screen.queryByTestId("cust-order-ORD-1")).toBeNull());
    resolveSecond({ ok: true, json: async () => ({ success: true, user_id: "u2", orders: [], degraded: "" }) });
  });

  it("请求失败(网络/鉴权)也走降级提示而不是静默空白", async () => {
    stub({}, false);
    render(<ContextPanel conv={CONV} />);
    const panel = await screen.findByTestId("customer-orders");
    expect(await within(panel).findByText(/加载客户订单失败/)).toBeInTheDocument();
  });
});
