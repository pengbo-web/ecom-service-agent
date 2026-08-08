import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// L3③(生成前进度事件):验证 ChatView 对 progress 帧的渲染——只保留最新一条,
// reply_delta 首字/终帧 reply 到达后清空,不与既有 reply_delta/reply 渲染冲突。

let capturedOnEvent: ((e: any) => void) | null = null;

vi.mock("@/lib/api", () => ({
  consolidateMemory: vi.fn(),
  getHistory: vi.fn(async () => []),
  listConversations: vi.fn(async () => []),
  getProduct: vi.fn(async () => null),
  login: vi.fn(),
  createUser: vi.fn(),
  setToken: vi.fn(),
  clearToken: vi.fn(),
  setUserId: vi.fn(),
}));

vi.mock("@/hooks/useChatStream", () => ({
  useChatStream: (opts: any) => {
    capturedOnEvent = opts.onEvent;
    return { send: vi.fn(), streaming: false };
  },
}));

import { ChatView } from "@/components/ChatView";

function sendUserTurn() {
  fireEvent.change(screen.getByPlaceholderText(/试试/), { target: { value: "查一下我的订单" } });
  fireEvent.click(screen.getByText("发送"));
}

describe("ChatView 生成前进度事件(L3③)", () => {
  it("渲染最新一条 progress 文案,后一条覆盖前一条", async () => {
    render(<ChatView sessionId="s1" userId="u1" onUserId={() => {}} onConversation={() => {}} />);
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());

    sendUserTurn();

    capturedOnEvent!({ type: "progress", stage: "understanding", message: "正在理解您的问题…" });
    await waitFor(() => expect(screen.getByTestId("progress-indicator")).toBeInTheDocument());
    expect(screen.getByText("正在理解您的问题…")).toBeInTheDocument();

    capturedOnEvent!({ type: "progress", stage: "retrieving", message: "正在为您查询订单和物流信息…" });
    await waitFor(() => expect(screen.getByText("正在为您查询订单和物流信息…")).toBeInTheDocument());
    // 旧阶段文案不应该还留着
    expect(screen.queryByText("正在理解您的问题…")).not.toBeInTheDocument();
  });

  it("reply_delta 首字到达后清空进度提示", async () => {
    render(<ChatView sessionId="s2" userId="u1" onUserId={() => {}} onConversation={() => {}} />);
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());

    sendUserTurn();
    capturedOnEvent!({ type: "progress", stage: "generating", message: "正在为您生成回复…" });
    await waitFor(() => expect(screen.getByTestId("progress-indicator")).toBeInTheDocument());

    capturedOnEvent!({ type: "reply_delta", content: "您的订单", first: true });
    await waitFor(() => expect(screen.getByText("您的订单")).toBeInTheDocument());
    expect(screen.queryByTestId("progress-indicator")).not.toBeInTheDocument();
  });

  it("没有 progress 事件时(旧后端/关开关)不渲染任何进度提示,行为与改造前一致", async () => {
    render(<ChatView sessionId="s3" userId="u1" onUserId={() => {}} onConversation={() => {}} />);
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());

    sendUserTurn();
    capturedOnEvent!({ type: "reply", content: "您好" });
    await waitFor(() => expect(screen.getByText("您好")).toBeInTheDocument());
    expect(screen.queryByTestId("progress-indicator")).not.toBeInTheDocument();
  });
});
