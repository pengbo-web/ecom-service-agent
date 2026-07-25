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
      { type: "recall", source: "kb", query: "退货运费谁承担", hits: [
        { doc: "退换货政策", section: "七天无理由", score: 0.62 },
        { doc: "会员权益", section: "钻石会员", score: 0.41 },
      ] },
    ]} defaultOpen />);
    expect(screen.getByText(/退换货政策\/七天无理由/)).toBeInTheDocument();
    expect(screen.getByText(/会员权益\/钻石会员/)).toBeInTheDocument();
    expect(screen.getByText(/退货运费谁承担/)).toBeInTheDocument();
  });
});
