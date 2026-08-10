import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ShopView } from "@/components/ShopView";
import { ProductThumb } from "@/components/ProductThumb";

// 空商品列表有两种完全不同的含义:这家店真的没有商品,或者商品服务连不上。
// 改造前两者都渲染成"暂无商品(确认 hmdp 后端在跑)"——实跑走查里,一次被
// HTTP_PROXY 劫持的内网调用就这样表现为"这家店是空的",没有报错、没有日志。

function stub(payload: unknown, ok = true) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (String(url).includes("/api/products")) {
      return { ok, status: ok ? 200 : 502, json: async () => payload };
    }
    return { ok: true, json: async () => ({}) };
  }));
}

describe("ShopView 降级可区分", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("服务连不上时给出故障横幅,而不是「暂无商品」", async () => {
    stub({ products: [], degraded: true, reason: "商品服务连不上(ReadTimeout)" });
    render(<ShopView onConsult={() => {}} onBuy={() => {}} />);
    const banner = await screen.findByTestId("shop-degraded");
    expect(banner.textContent).toContain("ReadTimeout");
    // 空态文案也要跟着换成排查指引,不能说"本店暂无在售商品"
    expect(await screen.findByText(/HTTP_PROXY/)).toBeInTheDocument();
  });

  it("真的没有商品时不报故障", async () => {
    stub({ products: [], degraded: false });
    render(<ShopView onConsult={() => {}} onBuy={() => {}} />);
    expect(await screen.findByText("本店暂无在售商品")).toBeInTheDocument();
    expect(screen.queryByTestId("shop-degraded")).toBeNull();
  });

  it("老服务端没有 degraded 字段时按未降级处理", async () => {
    // 缺字段就报故障会把正常的空店铺误报成事故;因为缺字段就丢商品更糟。
    // 标题别用单字:没有图时占位渲染的是商品名首字,单字标题会和它撞成两个匹配。
    stub({ products: [{ id: "1", title: "Nike 运动鞋", price: 1, stock: 1, image: "", description: "" }] });
    render(<ShopView onConsult={() => {}} onBuy={() => {}} />);
    expect(await screen.findByText("Nike 运动鞋")).toBeInTheDocument();
    expect(screen.queryByTestId("shop-degraded")).toBeNull();
  });

  it("HTTP 非 200 也算降级", async () => {
    stub({}, false);
    render(<ShopView onConsult={() => {}} onBuy={() => {}} />);
    expect(await screen.findByTestId("shop-degraded")).toBeInTheDocument();
  });

  it("加载中不显示「0 件商品」", async () => {
    // 那是一个还不知道的事实,写出来就是错的(改造前头部与主体会同时显示
    // 「0 件商品」和「加载中…」)。
    let resolve: (v: unknown) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise((r) => { resolve = r; })));
    render(<ShopView onConsult={() => {}} onBuy={() => {}} />);
    expect(screen.queryByText("0 件商品")).toBeNull();
    resolve({ ok: true, json: async () => ({ products: [], degraded: false }) });
    await waitFor(() => expect(screen.getByText("0 件商品")).toBeInTheDocument());
  });
});

describe("ProductThumb", () => {
  it("图加载失败时退到有设计的占位,而不是空白", () => {
    const { container } = render(<ProductThumb id="1" src="/imgs/x.jpg" title="Nike 运动鞋" />);
    const img = container.querySelector("img")!;
    expect(img).toBeInTheDocument();
    fireEvent.error(img);   // 需要 act 包裹,裸 dispatchEvent 的状态更新不会被冲刷
    // 失败后不再渲染 img,改渲染首字占位
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("N");
  });

  it("没有图时直接用占位", () => {
    const { container } = render(<ProductThumb id="2" title="戴森 V15 吸尘器" />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("戴");
  });

  it("同一商品的占位色稳定,不随渲染变化", () => {
    // 随机色会让同一件商品每次渲染换个颜色,那比灰底更糟。
    const a = render(<ProductThumb id="7" title="A" />).container.innerHTML;
    const b = render(<ProductThumb id="7" title="A" />).container.innerHTML;
    expect(a).toBe(b);
  });
});
