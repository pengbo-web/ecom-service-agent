import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetadataChips } from "@/components/MetadataChips";
import { AgentActivity } from "@/components/AgentActivity";

describe("MetadataChips", () => {
  it("渲染意图/置信度/转人工", () => {
    render(<MetadataChips meta={{ intent: "order_query", confidence: 0.83, requires_human: false }} />);
    expect(screen.getByText(/订单查询/)).toBeInTheDocument();
    expect(screen.getByText(/83%/)).toBeInTheDocument();
    expect(screen.getByText(/否/)).toBeInTheDocument();
  });
});

describe("AgentActivity", () => {
  it("渲染思考与工具事件", () => {
    render(<AgentActivity events={[
      { type: "thought", content: "先查订单" },
      { type: "tool_call", name: "query_order", args: { order_id: "X" } },
      { type: "tool_result", content: "{\"ok\":true}" },
    ]} defaultOpen />);
    expect(screen.getByText(/先查订单/)).toBeInTheDocument();
    expect(screen.getByText(/query_order/)).toBeInTheDocument();
  });
});

describe("AgentActivity recall", () => {
  it("渲染 KB 预召回事件(文档/章节)", () => {
    render(<AgentActivity events={[
      { type: "recall", source: "kb", backend: "aperag", query: "退货运费谁承担", hits: [
        { doc: "退换货政策", section: "七天无理由", score: 0.62 },
        { doc: "会员权益", section: "钻石会员", score: 0.41 },
      ] },
    ]} defaultOpen />);
    expect(screen.getByText(/退换货政策\/七天无理由/)).toBeInTheDocument();
    expect(screen.getByText(/会员权益\/钻石会员/)).toBeInTheDocument();
    expect(screen.getByText(/退货运费谁承担/)).toBeInTheDocument();
    expect(screen.getByText(/ApeRAG/)).toBeInTheDocument();   // 后端来源标识可见
  });
});

describe("AgentActivity 查询理解", () => {
  it("route 事件带意图与门控徽标", () => {
    render(<AgentActivity events={[
      { type: "route", agent: "售后服务专家", key: "aftersale", intent: "政策咨询", need_kb: true, source: "llm" },
    ]} defaultOpen />);
    expect(screen.getByText(/政策咨询/)).toBeInTheDocument();
    expect(screen.getByText(/需检索/)).toBeInTheDocument();
  });

  it("recall skipped 事件渲染跳过原因", () => {
    render(<AgentActivity events={[
      { type: "recall", source: "kb", skipped: true, reason: "闲聊寒暄" },
    ]} defaultOpen />);
    expect(screen.getByText(/跳过/)).toBeInTheDocument();
    expect(screen.getByText(/闲聊寒暄/)).toBeInTheDocument();
  });
});

describe("AgentActivity FAQ 秒答", () => {
  it("渲染缓存命中事件", () => {
    render(<AgentActivity events={[
      { type: "faq_cache", matched: "下单后多久发货", score: 0.95 },
    ]} defaultOpen />);
    expect(screen.getByText(/FAQ 秒答/)).toBeInTheDocument();
    expect(screen.getByText(/下单后多久发货/)).toBeInTheDocument();
  });
});
