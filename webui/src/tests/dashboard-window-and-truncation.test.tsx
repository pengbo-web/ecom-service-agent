/**
 * 看板:「最近请求」表格必须跟卡片同一个窗口,且截断要说出来。
 *
 * 实测缺陷(走查可观测看板时抓到):卡片走 `/api/metrics?window_hours=N`,表格走
 * `/api/traces?limit=50` —— **不带窗口**。选「近 1 小时」时卡片显示"总请求数 1"、
 * 页脚"统计口径:近 1 小时",紧接着的表格仍然是 50 行、跨度 40.9 小时。
 * 排查时点表格里一行看调用链,拿到的是 40 小时前那次。
 *
 * 修完窗口之后真实数据又露出第二处:选「近 7 天」卡片 145 条、表格 50 行——那是
 * limit 截的。表格封顶本身正常,但标题只写「最近请求」时运维照旧会觉得数字算错了。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { DashboardView } from "@/components/DashboardView";

const urls: string[] = [];

/** 完整的一份指标。缺字段会让 MetricCards 抛错、整页白屏——那是另一条断言
 *  (见本文件最后一个 describe),不该让每个窗口测试都踩到它。 */
const FULL_METRICS = {
  total_traces: 40, error_rate: 0, latency_p50_ms: 800, latency_p95_ms: 1500,
  ttft_p50_ms: 500, ttft_p95_ms: 900, sanitize_buffer_cost_ms: 10,
  tool_success_rate: 1, tool_calls: 5, guard_blocks: 0, block_rate: 0,
  guard_sanitizes: 0, handoffs: 0, escalation_rate: 0,
  kb_degraded_rate: 0, kb_degraded_turns: 0, kb_attempted_turns: 0,
  total_prompt_tokens: 100, total_completion_tokens: 50, est_cost_usd: 0.01,
  intent_distribution: { order_query: 3 },
};

function stub(total: number, rowCount: number) {
  vi.stubGlobal("fetch", vi.fn(async (url: any) => {
    const u = String(url);
    urls.push(u);
    if (u.includes("/api/metrics")) {
      return { ok: true, json: async () => ({ ...FULL_METRICS, total_traces: total }) };
    }
    if (u.includes("/api/traces")) {
      return { ok: true, json: async () => Array.from({ length: rowCount }, (_, i) => ({
        trace_id: "t" + i, session_id: "s1", user_input: "问",
        intent: "order_query", started_at: 1786532272 - i, latency_ms: 100,
        status: "ok", error: null,
      })) };
    }
    return { ok: true, json: async () => ({}) };
  }));
}

beforeEach(() => { vi.clearAllMocks(); urls.length = 0; localStorage.clear(); });

describe("表格与卡片同窗口", () => {
  it("默认窗口(24h)就把 window_hours 传给 /api/traces", async () => {
    stub(40, 40);
    render(<DashboardView sessionId="s1" />);
    await waitFor(() => expect(urls.some((u) => u.includes("/api/traces"))).toBe(true));
    const t = urls.find((u) => u.includes("/api/traces"))!;
    expect(t).toMatch(/window_hours=24/);
  });

  it("切窗口时表格跟着重取,且窗口值一致", async () => {
    stub(1, 1);
    render(<DashboardView sessionId="s1" />);
    await waitFor(() => expect(urls.some((u) => u.includes("/api/traces"))).toBe(true));
    urls.length = 0;

    fireEvent.click(screen.getByText("近 1 小时"));
    await waitFor(() => expect(urls.some((u) => u.includes("/api/traces"))).toBe(true));

    const metrics = urls.find((u) => u.includes("/api/metrics"))!;
    const traces = urls.find((u) => u.includes("/api/traces"))!;
    expect(metrics).toMatch(/window_hours=1\b/);
    expect(traces).toMatch(/window_hours=1\b/);
  });

  it("「全部」窗口传 0(= 全部历史,与 /api/metrics 同义)", async () => {
    stub(545, 50);
    render(<DashboardView sessionId="s1" />);
    await waitFor(() => expect(urls.some((u) => u.includes("/api/traces"))).toBe(true));
    urls.length = 0;
    fireEvent.click(screen.getByText("全部"));
    await waitFor(() => expect(urls.some((u) => u.includes("window_hours=0"))).toBe(true));
  });
});

describe("截断披露", () => {
  it("表格被 limit 截断时说出窗口内的真实条数", async () => {
    stub(145, 50);                       // 近 7 天:卡片 145,表格 50
    render(<DashboardView sessionId="s1" />);
    fireEvent.click(await screen.findByText("只看本会话"));   // 取消勾选,进全量口径

    const el = await screen.findByTestId("traces-truncated");
    expect(el.textContent).toMatch(/50/);
    expect(el.textContent).toMatch(/145/);
  });

  it("没被截断时不显示,不制造噪音", async () => {
    stub(40, 40);
    render(<DashboardView sessionId="s1" />);
    fireEvent.click(await screen.findByText("只看本会话"));
    await waitFor(() => expect(urls.some((u) => u.includes("/api/traces"))).toBe(true));
    expect(screen.queryByTestId("traces-truncated")).toBeNull();
  });

  it("只看本会话时不显示——卡片是全量口径、表格是单会话,本就不该相等", async () => {
    stub(145, 50);
    render(<DashboardView sessionId="s1" />);   // onlyMine 默认勾选
    await waitFor(() => expect(urls.some((u) => u.includes("session_id=s1"))).toBe(true));
    expect(screen.queryByTestId("traces-truncated")).toBeNull();
  });
});

describe("指标缺字段不该把看板打成白屏", () => {
  /** 走查时撞到:stub 少给了 latency 字段,`x.toFixed` 抛错,React 整棵子树渲染失败,
   *  body 里只剩一个空 div。而看板正是运维排故障时要看的那一页——白屏的时候连
   *  "后端还活着吗"都看不出来。缺失显示「—」(这个数没有),不用 0 兜(那是"这个数是零")。 */
  it("只给 total_traces 时仍然渲染出页面,缺的指标显示为 —", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: any) => {
      const u = String(url);
      if (u.includes("/api/metrics"))
        return { ok: true, json: async () => ({ total_traces: 7 }) };   // 故意残缺
      if (u.includes("/api/traces")) return { ok: true, json: async () => [] };
      return { ok: true, json: async () => ({}) };
    }));
    render(<DashboardView sessionId="s1" />);

    expect(await screen.findByText("总请求数")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
