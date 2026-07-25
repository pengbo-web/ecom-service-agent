import { useEffect, useState } from "react";
import { AppShell, type View } from "@/components/AppShell";
import { ChatView } from "@/components/ChatView";
import { DashboardView } from "@/components/DashboardView";
import { SeatView } from "@/components/SeatView";
import { EvalView } from "@/components/EvalView";
import { MemoryView } from "@/components/MemoryView";
import { LoginCard } from "@/components/LoginCard";
import { adminFetch, openConversation, getUserId, setUserId, me, clearToken } from "@/lib/api";

export default function App() {
  const [view, setView] = useState<View>(
    typeof location !== "undefined" && location.pathname === "/dashboard" ? "dash" : "chat"
  );
  const [userId, setUid] = useState<string>(getUserId());
  // 会话 ID 由服务端签发:挂载/切用户时 open(同用户已有 open 会话则复用,否则新开)。
  const [sessionId, setSessionId] = useState<string>("");
  // 登录门:启动时用已存 token 尝试恢复登录态,恢复失败(无 token/401)回登录卡片。
  const [authedUser, setAuthedUser] = useState<string | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    me().then((u) => {
      if (u) { setAuthedUser(u.user_id); setUid(u.user_id); setUserId(u.user_id); }
      else { clearToken(); }
    }).finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    if (!authedUser) return;
    let alive = true;
    openConversation(userId).then((c) => { if (alive) setSessionId(c.conversation_id); });
    return () => { alive = false; };
  }, [userId, authedUser]);

  function onUserId(uid: string) {
    const clean = uid.trim() || "default";
    setUserId(clean);
    setUid(clean);
  }

  async function onReset() {
    const r = await adminFetch("/api/session/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, user_id: userId }),
    });
    const data = await r.json();
    if (data?.conversation_id) setSessionId(data.conversation_id);   // 用响应里的新 ID 直接翻篇,不再手动重开
    setView("chat");
  }

  if (!authChecked) return <div className="p-8 text-sm text-muted-foreground">正在恢复登录…</div>;
  if (!authedUser) {
    return (
      <LoginCard
        onLogin={(uid) => {
          setAuthedUser(uid);
          setUid(uid);
          setUserId(uid);
        }}
      />
    );
  }
  if (!sessionId) return <div className="p-8 text-sm text-muted-foreground">正在建立会话…</div>;

  return (
    <AppShell view={view} onView={setView} onReset={onReset}>
      {view === "chat" && <ChatView sessionId={sessionId} userId={userId} onUserId={onUserId} onConversation={setSessionId} />}
      {view === "dash" && <DashboardView sessionId={sessionId} />}
      {view === "seat" && <SeatView sessionId={sessionId} />}
      {view === "eval" && <EvalView />}
      {view === "mem" && <MemoryView sessionId={sessionId} userId={userId} />}
    </AppShell>
  );
}
