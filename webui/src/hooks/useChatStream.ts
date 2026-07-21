import { useCallback, useState } from "react";
import { splitSSEFrames, type SSEEvent } from "@/lib/sse";

export function useChatStream(opts: { sessionId: string; onEvent: (e: SSEEvent) => void }) {
  const [streaming, setStreaming] = useState(false);

  const send = useCallback(async (message: string, confirm = false) => {
    setStreaming(true);
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: opts.sessionId, message, confirm }),
      });
      if (!resp.ok || !resp.body) throw new Error("服务返回 " + resp.status);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const { events, rest } = splitSSEFrames(buf);
        buf = rest;
        for (const e of events) opts.onEvent(e);
      }
    } catch (err: any) {
      opts.onEvent({ type: "error", message: err?.message || "连接失败" });
    } finally {
      setStreaming(false);
    }
  }, [opts]);

  return { send, streaming };
}
