import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetadataChips } from "@/components/MetadataChips";
import { EmotionDistributionCard } from "@/components/OperationsView";

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
