import { useCallback, useEffect, useRef, useState } from "react";
import { useChatStream } from "@/hooks/useChatStream";
import type { SSEEvent } from "@/lib/sse";
import { MessageBubble } from "@/components/MessageBubble";
import { AgentActivity } from "@/components/AgentActivity";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Send, RotateCcw, BarChart3, Search, AlertTriangle, Bell } from "lucide-react";
import { adminFetch, getSellerNotifications, markNotificationRead,
  type SellerNotification } from "@/lib/api";

// ---- 类型 ----

type ChatMsg = {
  id: number;
  role: "user" | "assistant";
  text: string;
  agent?: string;
  events?: SSEEvent[];     // Agent 思考过程事件(tool_call/route/stage 等)
  streaming?: boolean;     // 是否正在流式接收
};

type Anomaly = {
  kind: string;
  subject: string;
  subject_name: string;
  value: number;
  threshold: number;
  detail?: Record<string, unknown>;
};

// ---- 快捷提问 ----

const QUICK_ACTIONS = [
  { label: "📊 经营日报", question: "帮我出一份经营日报", icon: <BarChart3 className="h-4 w-4" /> },
  { label: "🏥 商品体检", question: "帮我做一次商品体检", icon: <Search className="h-4 w-4" /> },
  { label: "📉 退货归因", question: "退款率为什么升高了", icon: <AlertTriangle className="h-4 w-4" /> },
  { label: "🔍 异常扫描", question: "帮我扫描一下当前异常", icon: <AlertTriangle className="h-4 w-4" /> },
];

// ---- 主组件 ----

export function AnalystChatView({ sessionId }: { sessionId: string }) {
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState("");
  const [currentAgent, setCurrentAgent] = useState<string>("");
  const [progress, setProgress] = useState<string>("");
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [notifications, setNotifications] = useState<SellerNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [notifOpen, setNotifOpen] = useState(false);
  const idRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 会话 ID:复用同一个,参谋侧要能看到"这轮"之前的上下文
  const sellerSessionId = useRef(sessionId || `seller-${Date.now()}`).current;

  // 自动滚到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, progress]);

  // 加载异常摘要(左侧面板)
  useEffect(() => {
    adminFetch("/api/seller/overview?window_days=7")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d?.anomalies) setAnomalies(d.anomalies); })
      .catch(() => { /* 静默:异常面板是锦上添花 */ });
  }, []);

  // 通知轮询(30s 间隔)
  const refreshNotif = useCallback(() => {
    getSellerNotifications(false, 10)
      .then(d => { setNotifications(d.notifications); setUnreadCount(d.unread_count); })
      .catch(() => { /* 静默 */ });
  }, []);
  useEffect(() => {
    refreshNotif();
    const timer = setInterval(refreshNotif, 30_000);
    return () => clearInterval(timer);
  }, [refreshNotif]);

  // 回显已落盘的历史:离开页面再回来,聊天记录还在。
  //
  // 后端**一直在存**(app/sessions/seller/<sid>.json),此前只是没有端点能读回来——
  // 店主看到的是"记录丢了",实际是"存了但取不到"。会话 id 由 App.tsx 传成
  // `seller-<user>`(稳定值),所以同一个人回到这一页拿到的就是自己那条会话。
  //
  // `prev.length ? prev : ...`:请求在途时用户可能已经开始打字/发问了,
  // 那种情况下不能拿历史把现场覆盖掉。读失败也只是从空白开始,不打断使用。
  useEffect(() => {
    let cancelled = false;
    adminFetch(`/api/seller/${encodeURIComponent(sellerSessionId)}/history`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (cancelled || !d?.turns?.length) return;
        setMessages(prev => prev.length ? prev : d.turns.map(
          (t: { role: "user" | "assistant"; content: string }) => ({
            id: ++idRef.current, role: t.role, text: t.content,
          })));
      })
      .catch(() => { /* 历史读不到就从空白开始 */ });
    return () => { cancelled = true; };
  }, [sellerSessionId]);

  // SSE 事件处理
  const onEvent = useCallback((e: SSEEvent) => {
    switch (e.type) {
      case "route":
        setCurrentAgent(e.agent || e.key || "");
        break;
      case "progress":
        setProgress(e.message || e.stage || "分析中…");
        break;
      case "stage":
        if (e.status === "start") setProgress(`正在${e.name === "react" ? "分析" : e.name}…`);
        if (e.status === "end") setProgress("");
        break;
      case "reply_delta":
        // 流式追加到当前 assistant 消息
        setMessages(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant" && last.streaming) {
            return [...prev.slice(0, -1), { ...last, text: last.text + (e.content || "") }];
          }
          // 没有正在流式的 assistant 消息,创建一条
          return [...prev, {
            id: ++idRef.current, role: "assistant" as const,
            text: e.content || "", streaming: true, events: [],
          }];
        });
        break;
      case "reply":
        // 终帧:覆盖为完整回复,清除 streaming 标记
        setMessages(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant") {
            return [...prev.slice(0, -1), { ...last, text: e.content, streaming: false }];
          }
          return [...prev, {
            id: ++idRef.current, role: "assistant" as const,
            text: e.content, streaming: false, events: [],
          }];
        });
        setProgress("");
        break;
      case "metadata":
        // agent_key 用于标签
        if (e.agent_key) {
          const label = e.agent_key === "analyst" ? "参谋-小策" : e.agent_key === "growth" ? "增长-小拓" : e.agent_key;
          setCurrentAgent(label);
        }
        break;
      case "error":
        setErr(e.message || "未知错误");
        setProgress("");
        break;
      case "tool_call":
      case "tool_result":
      case "guard":
      case "faq_cache":
      case "recall":
      case "select":
      case "evaluate":
      case "polish":
        // 收集为 Agent 思考过程事件
        setMessages(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant" || !prev.length) {
            const target = last?.role === "assistant" ? last : {
              id: ++idRef.current, role: "assistant" as const,
              text: "", streaming: true, events: [],
            };
            const updated = { ...target, events: [...(target.events || []), e] };
            return last?.role === "assistant"
              ? [...prev.slice(0, -1), updated]
              : [...prev, updated];
          }
          return prev;
        });
        break;
    }
  }, []);

  const { send, streaming } = useChatStream({
    sessionId: sellerSessionId,
    userId: "seller",
    endpoint: "/api/seller/stream",
    onEvent,
  });

  function onSend(text?: string) {
    const msg = (text || draft).trim();
    if (!msg || streaming) return;
    setErr("");
    setMessages(prev => [...prev, { id: ++idRef.current, role: "user", text: msg }]);
    setDraft("");
    setCurrentAgent("");
    send(msg);
  }

  function onReset() {
    setMessages([]);
    setDraft("");
    setErr("");
    setCurrentAgent("");
    setProgress("");
  }

  return (
    <div className="flex h-full">
      {/* 左侧:快捷操作 + 异常摘要 */}
      <aside className="flex w-60 shrink-0 flex-col border-r bg-card/50">
        <div className="border-b p-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            快捷提问
          </h3>
          <div className="flex flex-col gap-1.5">
            {QUICK_ACTIONS.map(a => (
              <button
                key={a.label}
                className="flex items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm
                           transition-colors hover:bg-accent/10 disabled:opacity-50"
                disabled={streaming}
                onClick={() => onSend(a.question)}
              >
                {a.icon}
                <span>{a.label}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            当前异常（{anomalies.length}）
          </h3>
          {anomalies.length === 0 ? (
            <p className="text-xs text-muted-foreground">当前无跨线异常 ✅</p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {anomalies.map((a, i) => (
                <button
                  key={`${a.kind}-${a.subject}-${i}`}
                  className="rounded-md border p-2 text-left text-xs transition-colors
                             hover:bg-accent/10 disabled:opacity-50"
                  disabled={streaming}
                  onClick={() => onSend(`帮我分析一下${a.subject_name}的${a.kind}异常`)}
                >
                  <div className="font-medium text-foreground">
                    {a.subject_name}
                  </div>
                  <div className="mt-0.5 text-muted-foreground">
                    当前 {(a.value * 100).toFixed(1)}% · 告警线 {(a.threshold * 100).toFixed(1)}%
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </aside>

      {/* 右侧:对话区 */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* 顶栏 */}
        <div className="flex items-center gap-2 border-b px-4 py-2">
          <h2 className="text-sm font-semibold">参谋对话</h2>
          {currentAgent && (
            <Badge variant="outline" className="text-[11px]">
              {currentAgent}
            </Badge>
          )}
          <div className="ml-auto">
            <Button variant="ghost" size="sm" onClick={onReset}>
              <RotateCcw className="h-3.5 w-3.5" /> 清空
            </Button>
          </div>
        </div>

        {/* 通知条 */}
        {unreadCount > 0 && (
          <div className="border-b bg-amber-500/5">
            <button
              type="button"
              className="flex w-full items-center gap-2 px-4 py-1.5 text-left text-sm hover:bg-amber-500/10"
              onClick={() => setNotifOpen((v) => !v)}
            >
              <Bell className="h-3.5 w-3.5 text-amber-600" />
              <span className="text-amber-800 dark:text-amber-300">
                {unreadCount} 条新通知
              </span>
              <span className="text-[11px] text-muted-foreground">
                {notifOpen ? "收起" : "展开"}
              </span>
            </button>
            {notifOpen && (
              <div className="max-h-48 overflow-y-auto border-t border-amber-500/10 px-4 py-2">
                {notifications.map((n) => (
                  <button
                    key={n.id}
                    type="button"
                    className={`mb-1 block w-full rounded px-2 py-1.5 text-left text-xs hover:bg-muted/60 ${
                      n.read ? "text-muted-foreground" : "text-foreground"
                    }`}
                    onClick={async () => {
                      if (!n.read) {
                        try { await markNotificationRead(n.id); } catch { /* ignore */ }
                      }
                      setNotifications((prev) =>
                        prev.map((x) => (x.id === n.id ? { ...x, read: 1 } : x)),
                      );
                      setUnreadCount((c) => Math.max(0, c - (n.read ? 0 : 1)));
                      if (n.suggested_question) {
                        send(n.suggested_question);
                      }
                    }}
                  >
                    <span className="mr-1.5">{n.severity === "warning" ? "🔴" : "⚠️"}</span>
                    <span className="font-medium">{n.title}</span>
                    {n.summary && (
                      <span className="ml-2 text-muted-foreground">
                        {n.summary.length > 60 ? n.summary.slice(0, 60) + "…" : n.summary}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 消息列表 */}
        <div className="flex-1 overflow-y-auto" ref={scrollRef}>
          <div className="flex flex-col gap-2 p-4">
            {messages.length === 0 && (
              <div className="flex flex-col items-center gap-3 py-12 text-center text-sm text-muted-foreground">
                <div className="text-3xl">📊</div>
                <p>你好，我是经营参谋<strong>小策</strong></p>
                <p className="text-xs">
                  可以问我经营情况、商品体检、退货归因等问题，<br />
                  也可以点击左侧快捷按钮快速开始。
                </p>
              </div>
            )}

            {messages.map(m => (
              <div key={m.id} className={`flex flex-col ${m.role === "user" ? "items-end" : "items-start"}`}>
                {/* Agent 标签 */}
                {m.role === "assistant" && currentAgent && m === messages.filter(x => x.role === "assistant").at(-1) && (
                  <Badge variant="outline" className="mb-1 text-[10px]">
                    {currentAgent}
                  </Badge>
                )}

                {/* 消息气泡 */}
                <MessageBubble role={m.role} streaming={m.streaming}>
                  {m.text}
                </MessageBubble>

                {/* Agent 思考过程 */}
                {m.role === "assistant" && m.events && m.events.length > 0 && (
                  <AgentActivity events={m.events} defaultOpen={m.streaming} />
                )}
              </div>
            ))}

            {/* 进度指示 */}
            {progress && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                {progress}
              </div>
            )}

            {err && (
              <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
                ⚠️ {err}
              </div>
            )}
          </div>
        </div>

        {/* 输入区 */}
        <div className="flex items-end gap-2 border-t bg-card p-3">
          <textarea
            className="flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm
                       focus:outline-none focus:ring-2 focus:ring-ring"
            rows={1}
            placeholder="向参谋提问经营情况…"
            value={draft}
            disabled={streaming}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); } }}
          />
          <Button disabled={streaming || !draft.trim()} onClick={() => onSend()}>
            <Send className="h-4 w-4" /> {streaming ? "分析中…" : "发送"}
          </Button>
        </div>
      </div>
    </div>
  );
}
