import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// E1(回复流式化):验证 ChatView 对 reply_delta 的拼接渲染 + 终帧(reply)覆盖
// 校正——这条是前端侧"防丢块"的落地检查点,后端侧的拼接/护栏测试见
// tests/test_streaming_reply_delta.py。

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
  fireEvent.change(screen.getByPlaceholderText(/试试/), { target: { value: "我的订单呢" } });
  fireEvent.click(screen.getByText("发送"));
}

describe("ChatView 回复流式化(E1)", () => {
  it("reply_delta 逐块追加渲染打字光标,终帧 reply 覆盖并清掉光标", async () => {
    render(<ChatView sessionId="s1" userId="u1" onUserId={() => {}} onConversation={() => {}} />);
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());

    sendUserTurn();

    capturedOnEvent!({ type: "reply_delta", content: "您的", first: true });
    capturedOnEvent!({ type: "reply_delta", content: "订单" });
    await waitFor(() => expect(screen.getByText("您的订单")).toBeInTheDocument());
    // 流式中:打字光标可见
    expect(screen.getByTestId("reply-streaming-cursor")).toBeInTheDocument();

    // 终帧到达:即便与拼接结果不同(如护栏事后改写),也以终帧全文为准,并清掉光标
    capturedOnEvent!({ type: "reply", content: "您的订单已发货" });
    await waitFor(() => expect(screen.getByText("您的订单已发货")).toBeInTheDocument());
    expect(screen.queryByText("您的订单", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByTestId("reply-streaming-cursor")).not.toBeInTheDocument();
  });

  it("从未收到 reply_delta 的旧行为(仅终帧)保持不变", async () => {
    render(<ChatView sessionId="s2" userId="u1" onUserId={() => {}} onConversation={() => {}} />);
    await waitFor(() => expect(capturedOnEvent).not.toBeNull());

    sendUserTurn();
    capturedOnEvent!({ type: "reply", content: "您好,有什么可以帮您" });
    await waitFor(() => expect(screen.getByText("您好,有什么可以帮您")).toBeInTheDocument());
    expect(screen.queryByTestId("reply-streaming-cursor")).not.toBeInTheDocument();
  });
});
