import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { MetadataChips } from "@/components/MetadataChips";
import { EmotionDistributionCard } from "@/components/OperationsView";

// ---- ChatView 接线回归:metadata SSE 帧 → meta 对象 → MetadataChips 的整条
// 链路必须真的透传 emotion/emotion_level,而不是只有组件自己测过、接线没做
// (这正是本任务上一轮报告里发现的缺口)。用同款 vi.mock 拦截
// useChatStream,拿到 ChatView 传进去的 onEvent,手动喂 metadata 事件。
let capturedOnEvent: ((e: any) => void) | null = null;
vi.mock("@/hooks/useChatStream", () => ({
  useChatStream: (opts: any) => {
    capturedOnEvent = opts.onEvent;
    return { send: vi.fn(), streaming: false };
  },
}));
vi.mock("@/lib/api", () => ({
  consolidateMemory: vi.fn(),
  getHistory: vi.fn(async () => []),
  listConversations: vi.fn(async () => []),
  getProduct: vi.fn(),
  login: vi.fn(),
  createUser: vi.fn(),
  setToken: vi.fn(),
  clearToken: vi.fn(),
  setUserId: vi.fn(),
}));

const { ChatView } = await import("@/components/ChatView");

describe("MetadataChips 情绪标", () => {
  it("level 0 不渲染情绪标(中性不挂标,避免每条都是噪音)", () => {
    render(<MetadataChips meta={{
      intent: "product_consult", confidence: 0.9, requires_human: false,
      emotion: "neutral", emotion_level: 0,
    }} />);
    expect(screen.queryByText(/不满/)).not.toBeInTheDocument();
    expect(screen.queryByText(/情绪激烈/)).not.toBeInTheDocument();
  });

  it("level 1 不渲染情绪标", () => {
    render(<MetadataChips meta={{
      intent: "product_consult", confidence: 0.9, requires_human: false,
      emotion: "unhappy", emotion_level: 1,
    }} />);
    expect(screen.queryByText(/不满/)).not.toBeInTheDocument();
    expect(screen.queryByText(/情绪激烈/)).not.toBeInTheDocument();
  });

  it("level 3 渲染「情绪激烈」", () => {
    render(<MetadataChips meta={{
      intent: "complaint", confidence: 0.9, requires_human: true,
      emotion: "angry", emotion_level: 3,
    }} />);
    expect(screen.getByText("情绪激烈")).toBeInTheDocument();
  });
});

describe("EmotionDistributionCard 情绪分布卡", () => {
  it("total=0 时给空态文案", () => {
    render(<EmotionDistributionCard
      emotion={{ window_days: 7, total: 0, counts: { neutral: 0, unhappy: 0, angry: 0 }, angry_rate: 0 }}
      windowDays={7}
    />);
    expect(screen.getByText(/过去 7 天暂无会话/)).toBeInTheDocument();
  });

  it("无数据(emotion 缺失)时同样给空态文案", () => {
    render(<EmotionDistributionCard windowDays={7} />);
    expect(screen.getByText(/过去 7 天暂无会话/)).toBeInTheDocument();
  });

  it("有数据时渲染三档计数与激烈占比", () => {
    render(<EmotionDistributionCard
      emotion={{ window_days: 7, total: 4,
                counts: { neutral: 2, unhappy: 1, angry: 1 }, angry_rate: 0.25 }}
      windowDays={7}
    />);
    expect(screen.getByText(/激烈占比 25\.0%/)).toBeInTheDocument();
  });
});

describe("ChatView metadata 接线:emotion 从 SSE 帧到 MetadataChips", () => {
  it("metadata 帧带 emotion_level=3 时,聊天区渲染「情绪激烈」标", async () => {
    render(<ChatView sessionId="c-1" userId="u1" onUserId={() => {}} onConversation={() => {}} />);

    // 先走一轮真实发送:onSend 会把 cur.current 指向新 turn,后续事件才 patch 得到它
    fireEvent.change(screen.getByPlaceholderText(/我的订单还没发货/), { target: { value: "你们太坑了" } });
    fireEvent.click(screen.getByText("发送"));

    act(() => {
      capturedOnEvent!({ type: "reply", content: "非常抱歉" });
      capturedOnEvent!({
        type: "metadata", intent: "complaint", confidence: 0.9,
        requires_human: true, follow_up_question: null,
        emotion: "angry", emotion_level: 3,
      });
    });

    expect(screen.getByText("情绪激烈")).toBeInTheDocument();
  });

  it("metadata 帧不带 emotion 字段时按 neutral 兜底,不渲染情绪标(老后端/规则快筛轮次)", async () => {
    render(<ChatView sessionId="c-2" userId="u1" onUserId={() => {}} onConversation={() => {}} />);

    fireEvent.change(screen.getByPlaceholderText(/我的订单还没发货/), { target: { value: "你好" } });
    fireEvent.click(screen.getByText("发送"));

    act(() => {
      capturedOnEvent!({ type: "reply", content: "你好呀" });
      // 刻意不带 emotion/emotion_level——模拟规则快筛轮次或未升级的老后端
      capturedOnEvent!({
        type: "metadata", intent: "greeting", confidence: 0.99,
        requires_human: false, follow_up_question: null,
      });
    });

    expect(screen.getByText("你好呀")).toBeInTheDocument();   // 回复正常渲染
    expect(screen.queryByText(/不满/)).not.toBeInTheDocument();
    expect(screen.queryByText("情绪激烈")).not.toBeInTheDocument();
  });
});
