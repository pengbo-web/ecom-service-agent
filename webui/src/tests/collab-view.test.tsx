import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { CollabView } from "@/components/CollabView";

// 协作链视图。它补的是一个和"失败事件没有列表入口"同构的缺陷:
// `/api/admin/collab/timeline` 要求调用方先知道 correlation_id,而在链清单
// 端点之前,没有任何地方列出过它——"多 Agent 到底协作了什么"的唯一出口,
// 实际上只对恰好失败的链开放。

const HEALTH = {
  success: true, failed_count: 0, failed: [],
  worker: {
    name: "collab", last_success_at: "2026-08-10 12:00:00", last_error_at: null,
    last_error: null, stale_seconds: 30, threshold_seconds: 360, healthy: true,
  },
  budget: { limit: 200, spent: 0, enabled: true, scope: "本 API 进程计数",
            note: "实际消耗发生在协作 worker 进程,此处恒为 0" },
};

const ROUTING = {
  success: true,
  subscriptions: [
    { event_type: "signal.anomaly", target: "analyst", conditional: false,
      reason: "所有异常信号都由参谋归因;转人工类优先",
      priority: null, priority_dynamic: true, priority_label: "按事件内容动态判定" },
    { event_type: "insight.diagnosis", target: "growth", conditional: true,
      reason: "仅转化相关且归因未降级的诊断才唤醒营销",
      priority: 0, priority_dynamic: false, priority_label: "普通" },
  ],
  gates: [{ target: "growth", name: "marketing_paused", reason: "营销静默期:服务侧正在救火时,不同时推销。" }],
  agents: [
    { key: "service", label: "客服 Agent", side: "buyer", desc: "买家会话侧" },
    { key: "analyst", label: "参谋 Agent", side: "seller", desc: "只读经营归因" },
    { key: "growth", label: "营销 Agent", side: "seller", desc: "只产草稿" },
    { key: "human", label: "人工闸", side: "human", desc: "不可逆动作的唯一出口" },
  ],
};

const CHAINS = {
  success: true,
  chains: [{
    correlation_id: "C-abc123", events: 3,
    started_at: "2026-08-10 10:00:00", last_at: "2026-08-10 10:05:00",
    failed: 0, pending: 0, skipped: 1, agents: ["service", "analyst", "growth"],
  }],
};

const TIMELINE = {
  success: true,
  events: [
    { id: 1, event_type: "signal.anomaly", source_agent: "service", target_agent: "analyst",
      correlation_id: "C-abc123", status: "done", priority: 10,
      created_at: "2026-08-10 10:00:00", consumed_at: "2026-08-10 10:00:30",
      payload: { kind: "service_escalation", subject: "shop" } },
    { id: 2, event_type: "insight.diagnosis", source_agent: "analyst", target_agent: "growth",
      correlation_id: "C-abc123", status: "skipped", priority: 0,
      created_at: "2026-08-10 10:02:00", consumed_at: "2026-08-10 10:02:10", payload: {} },
  ],
  shared: [{
    key: "diagnosis:shop", value: { conclusion: "退款集中在尺码" },
    source_agent: "analyst", correlation_id: "C-abc123",
    updated_at: "2026-08-10 10:02:00", expires_at: null,
  }],
};

function stub(over: Record<string, unknown> = {}, onPost?: (u: string) => void) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (init?.method === "POST") {
      onPost?.(u);
      return { ok: true, json: async () => ({ success: true, changed: true, message: "ok" }) };
    }
    if (u.includes("collab/health")) return { ok: true, json: async () => over.health ?? HEALTH };
    if (u.includes("collab/routing")) return { ok: true, json: async () => over.routing ?? ROUTING };
    if (u.includes("collab/chains")) return { ok: true, json: async () => over.chains ?? CHAINS };
    if (u.includes("collab/timeline")) return { ok: true, json: async () => over.timeline ?? TIMELINE };
    return { ok: false, status: 404, json: async () => ({}) };
  }));
}

describe("CollabView 协作链", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("自动选中最近一条链并渲染时间线", async () => {
    // 一个需要先点一下才显示任何内容的时间线,大多数人只会看到空白面板然后离开。
    stub();
    render(<CollabView />);
    expect(await screen.findByTestId("timeline")).toBeInTheDocument();
    expect(await screen.findByTestId("event-1")).toBeInTheDocument();
  });

  it("事件按发生顺序渲染,不能倒序", async () => {
    // 后端给的是 id DESC。倒序会把因果读反——看起来像营销先起草、参谋才诊断。
    stub();
    render(<CollabView />);
    const tl = await screen.findByTestId("timeline");
    const ids = Array.from(tl.querySelectorAll("[data-testid^='event-']"))
      .map((el) => el.getAttribute("data-testid"));
    expect(ids).toEqual(["event-1", "event-2"]);
  });

  it("skipped 与 failed 分开呈现:跳过要说明是刻意不做", async () => {
    // 把"消费闸拦下"渲染成红色错误,会让运营去修一个根本不存在的故障。
    stub();
    render(<CollabView />);
    const ev = await screen.findByTestId("event-2");
    expect(within(ev).getByText("已跳过")).toBeInTheDocument();
    expect(within(ev).getByText(/刻意不做，?不是故障|刻意不做,不是故障/)).toBeInTheDocument();
  });

  it("展示 source→target 与优先级,普通档不挂徽章", async () => {
    stub();
    render(<CollabView />);
    const first = await screen.findByTestId("event-1");
    expect(within(first).getByText("客服 Agent")).toBeInTheDocument();
    expect(within(first).getByText("参谋 Agent")).toBeInTheDocument();
    expect(within(first).getByText(/优先级 紧急/)).toBeInTheDocument();
    // 每条都挂一个"普通"等于没有信息,只是噪声
    const second = await screen.findByTestId("event-2");
    expect(within(second).queryByText(/优先级/)).toBeNull();
  });

  it("等待时长按服务端两个时间戳相减,不拿浏览器时钟去减", async () => {
    // consumed_at 写在**认领**那一刻,所以这是排队等待而不是处理耗时。
    stub();
    render(<CollabView />);
    const first = await screen.findByTestId("event-1");
    expect(within(first).getByText(/等待 30s/)).toBeInTheDocument();
  });

  it("渲染该链写入的共享上下文", async () => {
    // 参谋结论流向客服侧的载体;不摆出来的话"跨 Agent 共享记忆"只是一句宣称。
    stub();
    render(<CollabView />);
    const shared = await screen.findByTestId("chain-shared");
    expect(within(shared).getByText("diagnosis:shop")).toBeInTheDocument();
    expect(within(shared).getByText(/退款集中在尺码/)).toBeInTheDocument();
  });

  it("时间线响应缺字段时只丢时间线,不把整页带崩", async () => {
    stub({ timeline: { success: true } });
    render(<CollabView />);
    expect(await screen.findByText(/响应格式不正确/)).toBeInTheDocument();
    // 链列表照常渲染
    expect(await screen.findByTestId("chain-C-abc123")).toBeInTheDocument();
  });

  it("链清单为空时给出怎么触发的提示,而不是一片空白", async () => {
    stub({ chains: { success: true, chains: [] } });
    render(<CollabView />);
    expect(await screen.findByText(/agent_collab/)).toBeInTheDocument();
  });

  it("失败事件可直接从健康条重试,打到正确端点", async () => {
    const posted: string[] = [];
    stub({
      health: {
        ...HEALTH, failed_count: 1,
        failed: [{ id: 9, event_type: "insight.diagnosis", source_agent: "analyst",
                   target_agent: "growth", correlation_id: "C-abc123",
                   created_at: "2026-08-10 10:02:00", consumed_at: null }],
      },
    }, (u) => posted.push(u));
    render(<CollabView />);
    const row = await screen.findByTestId("strip-failed-9");
    fireEvent.click(within(row).getByRole("button", { name: /放回队列重试/ }));
    await waitFor(() =>
      expect(posted.some((u) => u.includes("/api/admin/collab/failed/9/retry"))).toBe(true));
  });

  it("预算必须带上进程作用域说明,否则 spent=0 会被读成 worker 没花过钱", async () => {
    stub();
    render(<CollabView />);
    const strip = await screen.findByTestId("collab-health-strip");
    expect(within(strip).getByText(/LLM 预算 0\/200/)).toBeInTheDocument();
    expect(within(strip).getByText(/实际消耗发生在协作 worker 进程/)).toBeInTheDocument();
  });


  it("归因全部降级时明确报出来,并说明只看链颜色看不出这件事", async () => {
    // 实测:worker 报告 done:20 / failed:0,一片绿色,而 20 条归因全部降级。
    // 降级诊断连总线事件都不产生,链上只看到 signal.anomaly 变 done 然后断掉。
    stub({
      health: {
        ...HEALTH,
        degraded: { window: 50, diagnoses: 3, degraded: 3, rate: 1,
                    all_degraded: true,
                    note: "参谋归因的 LLM 调用失败时会降级为纯统计，协作链会在此静默断掉" },
      },
    });
    render(<CollabView />);
    const strip = await screen.findByTestId("degraded-strip");
    expect(within(strip).getByText(/本轮归因全部不可用/)).toBeInTheDocument();
    expect(within(strip).getByText(/只看协作链颜色看不出这件事/)).toBeInTheDocument();
  });

  it("没有降级时不显示这张卡,不制造噪声", async () => {
    stub({
      health: { ...HEALTH,
        degraded: { window: 50, diagnoses: 5, degraded: 0, rate: 0,
                    all_degraded: false, note: "x" } },
    });
    render(<CollabView />);
    await screen.findByTestId("collab-health-strip");
    expect(screen.queryByTestId("degraded-strip")).toBeNull();
  });

  it("老后端不带 degraded 字段时不崩", async () => {
    stub();
    render(<CollabView />);
    expect(await screen.findByTestId("collab-health-strip")).toBeInTheDocument();
    expect(screen.queryByTestId("degraded-strip")).toBeNull();
  });

  it("健康响应缺字段时只丢健康条,链列表照常", async () => {
    stub({ health: { success: true } });
    render(<CollabView />);
    expect(await screen.findByText(/响应格式不正确/)).toBeInTheDocument();
    expect(await screen.findByTestId("chain-C-abc123")).toBeInTheDocument();
  });
});

describe("CollabView 编排规则", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  async function openRules() {
    stub();
    render(<CollabView />);
    fireEvent.click(await screen.findByRole("button", { name: /编排规则/ }));
  }

  it("渲染 Agent 名册与订阅表(标签全部来自后端)", async () => {
    await openRules();
    expect(await screen.findByTestId("agent-analyst")).toBeInTheDocument();
    expect(await screen.findByTestId("sub-signal.anomaly")).toBeInTheDocument();
  });

  it("动态优先级如实标注,不挑一个档位糊弄", async () => {
    // 同一个 signal.anomaly,转人工是紧急、例行扫描是普通。摆一个"普通"会
    // 让人以为转人工也排在普通队列里,而那正是这条谓词存在的理由。
    await openRules();
    const row = await screen.findByTestId("sub-signal.anomaly");
    expect(within(row).getByText(/按事件内容动态判定/)).toBeInTheDocument();
  });

  it("带条件的订阅要标出来", async () => {
    await openRules();
    const row = await screen.findByTestId("sub-insight.diagnosis");
    expect(within(row).getByText("有条件")).toBeInTheDocument();
  });

  it("消费闸与订阅条件分区渲染,并说明两者失败方向相反", async () => {
    await openRules();
    expect(await screen.findByTestId("gate-growth")).toBeInTheDocument();
    expect(screen.getByText(/fail-closed/)).toBeInTheDocument();
    expect(screen.getByText(/fail-open/)).toBeInTheDocument();
  });

  it("说明路由是调度不是授权", async () => {
    // 这条不是文案洁癖:看到"加一行就能让新 Agent 收到事件"的人,很容易以为
    // 这张表也在发权限。
    await openRules();
    expect(await screen.findByText(/路由是调度，不是授权/)).toBeInTheDocument();
  });
});

describe("CollabView 人工待办", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  // 路由表把 drafts_ready / outreach_* 都投给 human,但 worker 只消费 analyst 与
  // growth——human 没有任何消费方。在这个页签之前也没有任何界面列出它们
  // (实测积压 325 条,永远 pending)。"留给人工"事实上是"留给没人"。
  const INBOX = {
    success: true, pending: 2,
    events: [
      { id: 9, event_type: "action.drafts_ready", source_agent: "growth",
        target_agent: "human", correlation_id: "C-x", status: "pending", priority: 0,
        created_at: "2026-08-10 10:00:00", consumed_at: null, payload: { drafted: 8 } },
      { id: 10, event_type: "result.outreach_converted", source_agent: "analyst",
        target_agent: "human", correlation_id: "C-y", status: "pending", priority: -10,
        created_at: "2026-08-10 10:01:00", consumed_at: null,
        payload: { draft_id: 1, outcome: "converted" } },
    ],
  };

  async function openInbox(over: Record<string, unknown> = {}, onPost?: (u: string) => void) {
    stub({ ...over }, onPost);
    const orig = globalThis.fetch as ReturnType<typeof vi.fn>;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes("collab/inbox") && (init?.method || "GET") === "GET") {
        return { ok: true, json: async () => (over.inbox ?? INBOX) };
      }
      return orig(url, init);
    }));
    render(<CollabView />);
    fireEvent.click(await screen.findByRole("button", { name: /人工待办/ }));
  }

  it("列出待办并在页签上显示条数", async () => {
    await openInbox();
    expect(await screen.findByTestId("inbox-9")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /人工待办（2）/ })).toBeInTheDocument();
  });

  it("说明「已确认」不等于批准或发送", async () => {
    // 两个动作长得像,但一个是划掉待办、另一个会真的把消息发到买家手机上。
    await openInbox();
    expect(await screen.findByText(/不代表批准或发送任何东西/)).toBeInTheDocument();
  });

  it("确认打到正确端点", async () => {
    const posted: string[] = [];
    await openInbox({}, (u) => posted.push(u));
    const row = await screen.findByTestId("inbox-9");
    fireEvent.click(within(row).getByRole("button", { name: /已确认/ }));
    await waitFor(() =>
      expect(posted.some((u) => u.includes("/api/admin/collab/inbox/9/ack"))).toBe(true));
  });

  it("没有待办时给空态而不是空白", async () => {
    await openInbox({ inbox: { success: true, pending: 0, events: [] } });
    expect(await screen.findByText("没有待办事件")).toBeInTheDocument();
  });

  it("响应缺字段时只丢这个页签,不把整页带崩", async () => {
    await openInbox({ inbox: { success: true } });
    expect(await screen.findByText(/响应格式不正确/)).toBeInTheDocument();
  });
});
