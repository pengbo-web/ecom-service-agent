import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { OperationsView } from "@/components/OperationsView";

const DATA = {
  overview: { success: true, window_days: 7, orders: 12, gmv: 4788, avg_order_value: 399,
              refunds: 3, refund_rate: 0.25, cancels: 0, cancel_rate: 0,
              conversations: 30, orders_per_conversation: 0.4 },
  products: { success: true, window_days: 7, products: [] },
  anomalies: [{ kind: "refund_rate_high", subject: "P001", subject_name: "跑鞋",
                value: 0.25, threshold: 0.15, detail: { orders: 12, refunds: 3,
                top_reason: "尺码不准" } }],
};

describe("OperationsView", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => DATA })));
    localStorage.clear();
  });

  it("渲染关键指标", async () => {
    render(<OperationsView />);
    expect(await screen.findByText(/25(\.0)?%/)).toBeInTheDocument();   // 退款率
    expect(await screen.findByText("12")).toBeInTheDocument();          // 订单量
  });

  it("异常显示当前值与告警线,且同一条异常内二者同时出现", async () => {
    render(<OperationsView />);
    const rows = await screen.findAllByTestId("anomaly-row");
    expect(rows).toHaveLength(1);
    const row = rows[0];
    // 商品名与"当前值 · 告警线"必须出现在同一条异常里,而不是页面上任意位置各自命中一次
    expect(within(row).getByText(/跑鞋/)).toBeInTheDocument();
    expect(within(row).getByText(/当前.*告警线/)).toBeInTheDocument();
  });

  it("每张指标卡都各自标出统计窗口,数字不带窗口对店主没意义", async () => {
    render(<OperationsView />);
    await screen.findByText("12"); // 等待加载完成
    const cards = await screen.findAllByTestId("metric-card");
    expect(cards).toHaveLength(5);
    // 5 张指标卡都应各自带上"近 7 天"的窗口标注,而不是只在共享大标题里出现一次
    const windowLabels = await screen.findAllByText(/近\s*7\s*天/);
    expect(windowLabels).toHaveLength(5);
    for (const card of cards) {
      expect(within(card).getByText(/近\s*7\s*天/)).toBeInTheDocument();
    }
  });

  it("异常明细里的嵌套数组要渲染成可读文本,不能出现 [object Object]", async () => {
    const DATA_NESTED = {
      ...DATA,
      anomalies: [{
        kind: "refund_rate_high", subject: "P002", subject_name: "T恤",
        value: 1 / 6, threshold: 0.15,
        detail: {
          orders: 20, refunds: 6, top_reason: "尺码不准,偏大一码",
          success_rate: 1 / 6,
          refund_reasons: [{ reason: "尺码不准,偏大一码", count: 6 }, { reason: "色差", count: 1 }],
        },
      }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => DATA_NESTED })));
    render(<OperationsView />);
    const row = await screen.findByTestId("anomaly-row");

    // 嵌套数组必须被拆成可读文本,绝不能整个对象被字符串化成 [object Object]
    expect(within(row).queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    expect(within(row).getByText(/尺码不准,偏大一码×6/)).toBeInTheDocument();
  });

  it("异常明细里的分数字段要按百分比渲染,不能吐出 17 位小数原始浮点数", async () => {
    const DATA_RATE = {
      ...DATA,
      anomalies: [{
        kind: "refund_rate_high", subject: "P003", subject_name: "外套",
        value: 1 / 6, threshold: 0.15,
        detail: { orders: 20, refunds: 6, success_rate: 1 / 6 },
      }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => DATA_RATE })));
    render(<OperationsView />);
    const row = await screen.findByTestId("anomaly-row");

    expect(within(row).queryByText(/0\.16666666666666666/)).not.toBeInTheDocument();
    // 与指标卡同款 pct() 格式:1 位小数的百分比
    expect(within(row).getByText(/success_rate:16\.7%/)).toBeInTheDocument();
  });

  it("无异常时给明确空态而不是留白", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, json: async () => ({ ...DATA, anomalies: [] }) })));
    render(<OperationsView />);
    expect(await screen.findByText(/无跨线异常/)).toBeInTheDocument();
  });

  it("刷新失败时旧数据要标过期,不能装作是最新数据(单实例内触发刷新失败)", async () => {
    const f = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => DATA })
      .mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) });
    vi.stubGlobal("fetch", f);
    render(<OperationsView />);
    await screen.findByText("12");

    // 成功渲染时不应该出现"过期"标记——否则一个永远显示"已过期"的组件也能骗过这条断言
    expect(screen.queryByText(/已过期|过期/)).not.toBeInTheDocument();

    // 在同一个挂载实例内触发刷新(点击"刷新"按钮),模拟这次刷新失败
    fireEvent.click(screen.getByRole("button", { name: /刷新/ }));

    // 失败后必须出现过期标记,且旧数据(12)依然原样可见,不能被清空或替换成空壳
    expect(await screen.findByText(/已过期|过期/)).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("快速切换统计窗口时,后到的旧响应不能覆盖已经到达的新响应", async () => {
    let resolveSlow!: (v: unknown) => void;
    let resolveFast!: (v: unknown) => void;
    const slow = new Promise((res) => { resolveSlow = res; });
    const fast = new Promise((res) => { resolveFast = res; });

    // 第一次 fetch(挂载时的 7 天请求)故意"慢",第二次 fetch(切到 14 天)"快"
    const f = vi.fn()
      .mockImplementationOnce(() => slow)
      .mockImplementationOnce(() => fast);
    vi.stubGlobal("fetch", f);

    render(<OperationsView />);
    // 挂载触发了第一次(7 天,慢)请求;紧接着切到 14 天,触发第二次(快)请求
    fireEvent.click(screen.getByText("14 天"));

    // 快的(14 天)响应先落地
    resolveFast({
      ok: true,
      json: async () => ({
        ...DATA,
        overview: { ...DATA.overview, window_days: 14, orders: 99 },
      }),
    });
    expect(await screen.findByText("99")).toBeInTheDocument();

    // 慢的(7 天,旧窗口)响应后落地——没有请求序号防护的话,它会覆盖上面已经
    // 显示的 14 天结果,店主就会看到自己已经切走的窗口的数字。等一整个宏任务
    // tick,确保这条迟到响应的 then/catch 链(包括内部的 r.json())已经跑完。
    resolveSlow({ ok: true, json: async () => DATA }); // orders: 12,对应 7 天
    await new Promise((r) => setTimeout(r, 0));

    // 仍应停留在最新一次点击(14 天)的结果上
    expect(screen.getByText("99")).toBeInTheDocument();
    expect(screen.queryByText("12")).not.toBeInTheDocument();
  });
});
