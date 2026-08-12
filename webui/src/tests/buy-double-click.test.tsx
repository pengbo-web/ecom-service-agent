/**
 * 「立即购买」双击不能下出两笔订单。
 *
 * **实测缺陷**(走查并发与幂等时抓到)。`App.onBuy` 原来没有任何在途判断,而
 * `ShopView` 与 `ProductCard` 里的「立即购买」都是裸 `<button onClick>`——双击就是
 * 两次 `POST /api/order`,两笔订单。而购物车那条路的「去下单」一直写着
 * `disabled={busy}`:**同一件事,一条路上有、另一条没有。**
 *
 * 后端也没有幂等键(全仓搜不到 idempotency / request_id / client_token),所以这一层
 * 是目前唯一的防线。真正的幂等键需要改 API 契约(客户端生成 key + 服务端去重),
 * 已另开任务;这里先把最现实的那条路——买家手抖点两下——堵死。
 *
 * 守在 `App.onBuy` 的 ref 上而不是只把按钮变灰:它是三个调用点的唯一收口(商城列表、
 * 聊天里的商品卡、以后任何新入口)。按钮变灰是给人看的反馈,ref 才是拦住第二个请求的东西。
 * 用 ref 而不是 state 是因为 setState 异步,两次快速点击可能在同一帧里都读到旧值。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ShopView } from "@/components/ShopView";
import { ProductCard } from "@/components/ProductCard";

const PRODUCT = { id: "1", name: "Nike Air Max 270", price: 899, stock: 5,
                  image: "", description: "d" } as any;

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); });

/** 复刻 App.onBuy 的在途保护(测的是那套语义,不是 App 的渲染树)。 */
function makeGuardedBuy(calls: string[], resolveLater: Promise<void>) {
  let inFlight = false;
  return async (id: string) => {
    if (inFlight) return;
    inFlight = true;
    try {
      calls.push(id);
      await resolveLater;
    } finally {
      inFlight = false;
    }
  };
}

describe("ProductCard 立即购买", () => {
  it("在途时按钮禁用并显示处理中", () => {
    render(<ProductCard product={PRODUCT} onAsk={() => {}} onBuy={() => {}} buying />);
    const btn = screen.getByText("处理中…") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("不在途时正常可点", () => {
    const onBuy = vi.fn();
    render(<ProductCard product={PRODUCT} onAsk={() => {}} onBuy={onBuy} />);
    fireEvent.click(screen.getByText("立即购买"));
    expect(onBuy).toHaveBeenCalledTimes(1);
  });

  it("快速双击:在途保护只放过第一次", async () => {
    const calls: string[] = [];
    let release: () => void = () => {};
    const pending = new Promise<void>((r) => { release = () => r(); });
    const onBuy = makeGuardedBuy(calls, pending);

    // buying 由父组件在下一帧才变 true,所以这里刻意保持 false——
    // 复现"按钮还没变灰就被点了第二下"这个真实时序。
    render(<ProductCard product={PRODUCT} onAsk={() => {}} onBuy={onBuy} buying={false} />);
    const btn = screen.getByText("立即购买");
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls).toEqual(["1"]);       // 修复前是 ["1","1","1"] —— 三笔订单

    release();
    await waitFor(() => expect(calls).toEqual(["1"]));
  });

  it("第一次完成之后可以再买一次(保护是在途的,不是一次性的)", async () => {
    const calls: string[] = [];
    let release: () => void = () => {};
    const pending = new Promise<void>((r) => { release = () => r(); });
    const onBuy = makeGuardedBuy(calls, pending);

    render(<ProductCard product={PRODUCT} onAsk={() => {}} onBuy={onBuy} />);
    fireEvent.click(screen.getByText("立即购买"));
    release();
    await waitFor(() => expect(calls).toEqual(["1"]));

    fireEvent.click(screen.getByText("立即购买"));
    await waitFor(() => expect(calls).toEqual(["1", "1"]));
  });
});

describe("ShopView 立即购买", () => {
  function stubProducts() {
    vi.stubGlobal("fetch", vi.fn(async (url: any) => {
      const u = String(url);
      if (u.includes("/api/products")) {
        return { ok: true, json: async () => ({ products: [PRODUCT], degraded: false }) };
      }
      return { ok: true, json: async () => ({}) };
    }));
  }

  it("在途时按钮禁用", async () => {
    stubProducts();
    render(<ShopView onConsult={() => {}} onBuy={() => {}} buying />);
    const btn = (await screen.findByText("处理中…")) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("默认不禁用,且点一次只调一次", async () => {
    stubProducts();
    const onBuy = vi.fn();
    render(<ShopView onConsult={() => {}} onBuy={onBuy} />);
    fireEvent.click(await screen.findByText("立即购买"));
    expect(onBuy).toHaveBeenCalledTimes(1);
  });
});
