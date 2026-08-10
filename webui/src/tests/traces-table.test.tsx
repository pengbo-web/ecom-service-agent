import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { TracesTable, type Trace } from "@/components/TracesTable";

const TRACES: Trace[] = [
  { trace_id: "t1", started_at: 100, session_id: "sess1", intent: "after_sale",
    status: "ok", latency_ms: 1200, prompt_tokens: 80, completion_tokens: 20 },
];

// W1:一条真实形状的调用链——react(含一次被工作流守卫拦截的工具调用)
// 后接 reply_pipeline(含一次降级)，验证嵌套顺序、真实时长、以及
// workflow_guard/degrade 两种醒目标记都能在详情面板里看到。
const DETAIL = {
  trace_id: "t1", user_input: "申请退款", intent: "after_sale", status: "ok",
  spans: [
    { span_id: "s-react", kind: "stage", name: "stage:react", latency_ms: 340,
      parent_span_id: null },
    { span_id: "s-tool", kind: "tool", name: "tool:refund", latency_ms: 5,
      success: false, parent_span_id: "s-react" },
    { span_id: "s-guard", kind: "workflow_guard", name: "workflow_guard:refund",
      latency_ms: 0, parent_span_id: "s-react" },
    { span_id: "s-pipeline", kind: "stage", name: "stage:reply_pipeline",
      latency_ms: 210, parent_span_id: null },
    { span_id: "s-degrade", kind: "degrade", name: "degrade:empty_reply",
      latency_ms: 0, parent_span_id: "s-pipeline" },
  ],
};

describe("TracesTable", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("点击一行后展示嵌套的 stage 树与各阶段耗时", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => DETAIL })));
    render(<TracesTable traces={TRACES} />);
    fireEvent.click(screen.getByText("after_sale"));

    expect(await screen.findByText("stage:react")).toBeInTheDocument();
    expect(await screen.findByText("stage:reply_pipeline")).toBeInTheDocument();
    // 阶段耗时真实可见(不是 0ms 占位)
    expect(screen.getByText("340ms")).toBeInTheDocument();
    expect(screen.getByText("210ms")).toBeInTheDocument();
  });

  it("workflow_guard 与 degrade 用独立醒目标记，不与普通步骤混在一起", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => DETAIL })));
    render(<TracesTable traces={TRACES} />);
    fireEvent.click(screen.getByText("after_sale"));

    expect(await screen.findByText(/工作流拦截/)).toBeInTheDocument();
    expect(await screen.findByText(/降级/)).toBeInTheDocument();
    // 普通 kind 仍按 [kind] 呈现，没被醒目样式吞掉判别力
    expect(screen.getByText("[tool]")).toBeInTheDocument();
  });

  it("加载失败时展示错误而不是让弹窗空白", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network down"); }));
    render(<TracesTable traces={TRACES} />);
    fireEvent.click(screen.getByText("after_sale"));

    expect(await screen.findByText(/加载调用链失败/)).toBeInTheDocument();
  });

  it("暂无记录时表格给出空态提示", () => {
    render(<TracesTable traces={[]} />);
    expect(screen.getByText("暂无记录")).toBeInTheDocument();
  });

  // 看板显示"三成工具调用在失败",而点开 trace 只有一个 ❌ ——这个结论就查不
  // 下去了。失败原因由后端写进 span.meta.error(只取原因字段、截断 200 字符)。
  it("工具失败时把原因显示出来,不是只给一个 ❌", async () => {
    const detail = {
      ...DETAIL,
      spans: [{ span_id: "s1", kind: "tool", name: "tool:query_order", latency_ms: 22,
                success: 0, parent_span_id: null,
                meta: { args: {}, error: "未找到订单 ORD-20240115-001，请核实订单号" }}],
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => detail })));
    render(<TracesTable traces={TRACES} />);
    fireEvent.click(screen.getByText("after_sale"));
    expect(await screen.findByText(/未找到订单 ORD-20240115-001/)).toBeInTheDocument();
  });

  it("success 为数字 0 也算失败(库里是 INTEGER,不是布尔)", async () => {
    // 这条钉的是一个已经踩过的坑:`span.success === false` 对 `0` 恒不成立,
    // 于是 ✅/❌ 图标(真值判断)正常、原因却不显示——两处判据不一致最难看出来。
    const detail = {
      ...DETAIL,
      spans: [{ span_id: "s1", kind: "tool", name: "tool:x", latency_ms: 1,
                success: 0, parent_span_id: null, meta: { error: "订单不存在" }}],
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => detail })));
    render(<TracesTable traces={TRACES} />);
    fireEvent.click(screen.getByText("after_sale"));
    expect(await screen.findByText("订单不存在")).toBeInTheDocument();
  });

  it("成功的 span 不显示 error(即使数据里带了)", async () => {
    const detail = {
      ...DETAIL,
      spans: [{ span_id: "s1", kind: "tool", name: "tool:x", latency_ms: 1,
                success: 1, parent_span_id: null, meta: { error: "不该出现" }}],
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => detail })));
    render(<TracesTable traces={TRACES} />);
    fireEvent.click(screen.getByText("after_sale"));
    expect(await screen.findByText("tool:x")).toBeInTheDocument();
    expect(screen.queryByText("不该出现")).toBeNull();
  });

  it("旧数据没有 meta 时不报错", async () => {
    const detail = {
      ...DETAIL,
      spans: [{ span_id: "s1", kind: "tool", name: "tool:x", latency_ms: 1, success: 0,
                parent_span_id: null }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => detail })));
    render(<TracesTable traces={TRACES} />);
    fireEvent.click(screen.getByText("after_sale"));
    expect(await screen.findByText("tool:x")).toBeInTheDocument();
  });
});
