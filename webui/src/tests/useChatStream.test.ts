import { describe, it, expect, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useChatStream } from "@/hooks/useChatStream";

function mockSSEResponse(frames: string[]) {
  const enc = new TextEncoder();
  let i = 0;
  const body = {
    getReader() {
      return {
        read() {
          if (i < frames.length) return Promise.resolve({ value: enc.encode(frames[i++]), done: false });
          return Promise.resolve({ value: undefined, done: true });
        },
      };
    },
  };
  return { ok: true, body } as unknown as Response;
}

describe("useChatStream", () => {
  it("把 SSE 事件回调出来", async () => {
    const events: any[] = [];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockSSEResponse([
      'data: {"type":"thought","content":"想一下"}\n\n',
      'data: {"type":"reply","content":"你好"}\n\ndata: {"type":"metadata","intent":"greeting","confidence":0.9,"requires_human":false}\n\n',
      'data: {"type":"done"}\n\n',
    ])));
    const { result } = renderHook(() => useChatStream({ sessionId: "s1", userId: "default", onEvent: (e) => events.push(e) }));
    await act(async () => { await result.current.send("在吗"); });
    await waitFor(() => expect(events.at(-1)?.type).toBe("done"));
    expect(events.map((e) => e.type)).toContain("reply");
    expect(events.find((e) => e.type === "metadata").intent).toBe("greeting");
    vi.unstubAllGlobals();
  });
});
