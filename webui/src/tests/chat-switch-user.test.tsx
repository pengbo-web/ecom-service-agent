import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// 按用户返回不同会话列表:验证切换用户后历史面板不再残留上一用户的缓存。
const listConversations = vi.fn(async (userId: string) =>
  userId === "123"
    ? [{ conversation_id: "c-123aaa", status: "open", close_reason: null } as any]
    : [{ conversation_id: "c-2bbb", status: "open", close_reason: null } as any]
);

vi.mock("@/lib/api", () => ({
  consolidateMemory: vi.fn(),
  getHistory: vi.fn(async () => []),
  listConversations: (uid: string) => listConversations(uid),
  login: vi.fn(),
  createUser: vi.fn(),
  setToken: vi.fn(),
  clearToken: vi.fn(),
  setUserId: vi.fn(),
}));

vi.mock("@/hooks/useChatStream", () => ({
  useChatStream: () => ({ send: vi.fn(), streaming: false }),
}));

import { ChatView } from "@/components/ChatView";

describe("ChatView 切换用户", () => {
  beforeEach(() => listConversations.mockClear());

  it("切换用户后收起历史面板并清掉上一用户的会话缓存", async () => {
    const { rerender } = render(
      <ChatView sessionId="c-123aaa" userId="123" onUserId={() => {}} onConversation={() => {}} />
    );

    // 用户 123 打开历史面板 → 拉到 123 的会话
    fireEvent.click(screen.getByText("历史会话"));
    await waitFor(() => expect(screen.getByText(/c-123aaa/)).toBeInTheDocument());
    expect(listConversations).toHaveBeenCalledWith("123");

    // 切换到用户 2(App 换发新 userId prop)→ 面板必须重置,不得残留 123 的列表
    rerender(
      <ChatView sessionId="c-2bbb" userId="2" onUserId={() => {}} onConversation={() => {}} />
    );
    await waitFor(() => expect(screen.queryByText(/c-123aaa/)).not.toBeInTheDocument());
    // 面板收起(回到「历史会话」按钮态,而非「收起历史」)
    expect(screen.getByText("历史会话")).toBeInTheDocument();
    expect(screen.queryByText("收起历史")).not.toBeInTheDocument();
  });
});
