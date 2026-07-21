import { describe, it, expect } from "vitest";
import { splitSSEFrames } from "@/lib/sse";

describe("splitSSEFrames", () => {
  it("拆出完整帧，保留半帧余量", () => {
    const { events, rest } = splitSSEFrames(
      'data: {"type":"thought","content":"a"}\n\ndata: {"type":"reply","content":"hi"}\n\ndata: {"type":"do'
    );
    expect(events).toEqual([
      { type: "thought", content: "a" },
      { type: "reply", content: "hi" },
    ]);
    expect(rest).toBe('data: {"type":"do');
  });

  it("无完整帧时全部留作余量", () => {
    const { events, rest } = splitSSEFrames('data: {"type":"x"');
    expect(events).toEqual([]);
    expect(rest).toBe('data: {"type":"x"');
  });
});
