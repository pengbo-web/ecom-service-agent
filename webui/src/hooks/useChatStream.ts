import { useCallback, useState } from "react";
import { splitSSEFrames, type SSEEvent } from "@/lib/sse";
import { authHeaders } from "@/lib/api";

export function useChatStream(opts: {
  sessionId: string; userId: string; currentItemId?: string;
  endpoint?: string;  // 默认 "/api/chat"(买家);参谋传 "/api/seller/stream"
  onEvent: (e: SSEEvent) => void;
}) {
  const [streaming, setStreaming] = useState(false);
  const url = opts.endpoint || "/api/chat";
  // 非默认端点(如卖家侧)走 admin_auth,需携带 X-Admin-Token
  const isAdmin = url !== "/api/chat";

  const send = useCallback(async (message: string, confirm = false) => {
    setStreaming(true);
    try {
      const baseHeaders = { "Content-Type": "application/json", ...authHeaders() };
      // 卖家端点额外携带 admin_token(与 adminFetch 同一口径)
      const headers = isAdmin
        ? { ...baseHeaders, ...(localStorage.getItem("admin_token") ? { "X-Admin-Token": localStorage.getItem("admin_token")! } : {}) }
        : baseHeaders;
      const resp = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({ session_id: opts.sessionId, message, confirm, user_id: opts.userId,
          current_item_id: opts.currentItemId || "" }),
      });
      if (resp.status === 401) {
        opts.onEvent({ type: "error", status: 401, message: "登录已过期" });
        return;
      }
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
  }, [opts, url, isAdmin]);

  return { send, streaming };
}
